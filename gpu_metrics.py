"""Read-only Windows GPU telemetry; no model imports or device configuration.

PDH engine data measures the interval between calls. NVML device utilization uses
the driver's own recent sampling window, so those two percentages need not agree.
GPU indexes below are NVIDIA enumeration indexes, not Windows Task Manager indexes.
The engine keys retain Windows adapter LUID + physical adapter + engine identity.
"""

from __future__ import annotations

import csv
import ctypes as ct
from ctypes import wintypes as wt
import io
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time


_GIB = 1024 ** 3
_PDH_MORE_DATA = 0x800007D2
_PDH_FMT_DOUBLE = 0x00000200
_ENGINE_RE = re.compile(
    r"^pid_(?P<pid>\d+)_luid_(?P<luid>0x[0-9a-f]+_0x[0-9a-f]+)"
    r"_phys_(?P<phys>\d+)_eng_(?P<eng>\d+)_engtype_(?P<type>.+?)(?:#\d+)?$",
    re.IGNORECASE,
)


class _PdhValueUnion(ct.Union):
    _fields_ = [("longValue", ct.c_int32), ("doubleValue", ct.c_double),
                ("largeValue", ct.c_int64), ("AnsiStringValue", ct.c_char_p),
                ("WideStringValue", ct.c_wchar_p)]


class _PdhValue(ct.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("CStatus", ct.c_uint32), ("value", _PdhValueUnion)]


class _PdhItem(ct.Structure):
    _fields_ = [("szName", ct.c_wchar_p), ("FmtValue", _PdhValue)]


class _NvmlUtilization(ct.Structure):
    _fields_ = [("gpu", ct.c_uint), ("memory", ct.c_uint)]


class _NvmlMemory(ct.Structure):
    _fields_ = [("total", ct.c_ulonglong), ("free", ct.c_ulonglong),
                ("used", ct.c_ulonglong)]


def _pct(value):
    """Missing/non-finite telemetry stays missing, never becomes idle (zero)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(100.0, max(0.0, number)), 3) if math.isfinite(number) else None


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


class GpuSampler:
    """Keep one PDH query alive; call sample about every 3 seconds then close.

    Missing PIDs have no valid GPU counter sample; callers must not silently turn
    them into measured zeroes. ``available`` means at least one source succeeded;
    ``processes_available`` separately indicates whether PDH returned valid data.
    No subprocesses are spawned on the normal NVML + PDH path.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._closed = False
        self._query = ct.c_void_p()
        self._counter = ct.c_void_p()
        self._pdh = None
        self._pdh_error = None
        self._pdh_last_collect = None
        self._nvml = None
        self._nvml_devices = []
        self._nvml_error = None
        self._smi = None
        if os.name != "nt":
            self._pdh_error = "Windows GPU counters are unavailable on this platform."
            self._nvml_error = "This sampler is configured for Windows."
            return
        self._init_pdh()
        self._init_nvml()
        system_smi = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        self._smi = str(system_smi) if system_smi.is_file() else shutil.which("nvidia-smi")

    def _init_pdh(self):
        try:
            pdh = ct.WinDLL("pdh.dll")
            signatures = {
                "PdhOpenQueryW": [wt.LPCWSTR, ct.c_size_t, ct.POINTER(ct.c_void_p)],
                "PdhAddEnglishCounterW": [ct.c_void_p, wt.LPCWSTR, ct.c_size_t, ct.POINTER(ct.c_void_p)],
                "PdhCollectQueryData": [ct.c_void_p],
                "PdhGetFormattedCounterArrayW": [ct.c_void_p, ct.c_uint32,
                    ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32), ct.c_void_p],
                "PdhCloseQuery": [ct.c_void_p],
            }
            for name, args in signatures.items():
                fn = getattr(pdh, name)
                fn.argtypes, fn.restype = args, ct.c_uint32
            self._pdh = pdh
            rc = pdh.PdhOpenQueryW(None, 0, ct.byref(self._query))
            if rc:
                raise RuntimeError(f"PdhOpenQueryW: 0x{rc:08X}")
            rc = pdh.PdhAddEnglishCounterW(
                self._query, r"\GPU Engine(*)\Utilization Percentage", 0,
                ct.byref(self._counter))
            if rc:
                raise RuntimeError(f"PdhAddEnglishCounterW: 0x{rc:08X}")
            rc = pdh.PdhCollectQueryData(self._query)
            if rc:
                # Some driver versions have no current instances. Retry on sample.
                self._pdh_last_collect = None
            else:
                self._pdh_last_collect = time.monotonic()
        except (OSError, AttributeError, RuntimeError) as exc:
            self._pdh_error = "GPU process counters unavailable: " + str(exc)
            if self._pdh and self._query:
                self._pdh.PdhCloseQuery(self._query)
                self._query = ct.c_void_p()
            self._pdh = None

    def _init_nvml(self):
        candidates = [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvml.dll",
                      Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvml.dll"]
        dll = None
        initialized = False
        try:
            for path in candidates:
                if not path.is_file():
                    continue
                try:
                    dll = ct.CDLL(str(path))
                    break
                except OSError:
                    continue
            if dll is None:
                raise RuntimeError("NVML library not found")
            signatures = {
                "nvmlInit_v2": [], "nvmlShutdown": [],
                "nvmlDeviceGetCount_v2": [ct.POINTER(ct.c_uint)],
                "nvmlDeviceGetHandleByIndex_v2": [ct.c_uint, ct.POINTER(ct.c_void_p)],
                "nvmlDeviceGetName": [ct.c_void_p, ct.c_void_p, ct.c_uint],
                "nvmlDeviceGetUtilizationRates": [ct.c_void_p, ct.POINTER(_NvmlUtilization)],
                "nvmlDeviceGetMemoryInfo": [ct.c_void_p, ct.POINTER(_NvmlMemory)],
                "nvmlDeviceGetTemperature": [ct.c_void_p, ct.c_uint, ct.POINTER(ct.c_uint)],
            }
            for name, args in signatures.items():
                fn = getattr(dll, name)
                fn.argtypes, fn.restype = args, ct.c_int
            rc = dll.nvmlInit_v2()
            if rc:
                raise RuntimeError(f"NVML initialization status {rc}")
            initialized = True
            count = ct.c_uint()
            rc = dll.nvmlDeviceGetCount_v2(ct.byref(count))
            if rc or not count.value:
                raise RuntimeError(f"NVML device enumeration status {rc}")
            for index in range(min(count.value, 32)):
                handle = ct.c_void_p()
                rc = dll.nvmlDeviceGetHandleByIndex_v2(index, ct.byref(handle))
                if rc:
                    continue
                name = ct.create_string_buffer(256)
                rc = dll.nvmlDeviceGetName(handle, name, len(name))
                device_name = name.value.decode("utf-8", errors="replace") if not rc else f"NVIDIA GPU {index}"
                self._nvml_devices.append((index, handle, device_name))
            if not self._nvml_devices:
                raise RuntimeError("No accessible NVML device handles")
            self._nvml = dll
        except (OSError, AttributeError, RuntimeError) as exc:
            self._nvml_error = str(exc)
            self._nvml_devices = []
            if dll is not None and initialized:
                dll.nvmlShutdown()

    def _sample_engines(self):
        if not self._pdh or not self._query:
            return {}, False, [self._pdh_error or "GPU process counters unavailable."]
        now = time.monotonic()
        if self._pdh_last_collect is not None and now - self._pdh_last_collect < 0.1:
            return {}, False, ["GPU process counters are warming up; wait for the next sample."]
        rc = self._pdh.PdhCollectQueryData(self._query)
        if rc:
            return {}, False, [f"GPU process collection unavailable: 0x{rc:08X}"]
        previous = self._pdh_last_collect
        self._pdh_last_collect = now
        if previous is None:
            return {}, False, ["GPU process counters are warming up; wait for the next sample."]
        # Query twice per the documented buffer-size contract; retry at most once
        # if process churn increased the required array size between these calls.
        for _ in range(2):
            size, count = ct.c_uint32(), ct.c_uint32()
            rc = self._pdh.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE, ct.byref(size), ct.byref(count), None)
            if rc == 0 and count.value == 0:
                return {}, True, []
            if rc != _PDH_MORE_DATA:
                return {}, False, [f"GPU process array unavailable: 0x{rc:08X}"]
            if not 0 < size.value <= 32 * 1024 * 1024:
                return {}, False, ["GPU process array exceeds the safe buffer limit."]
            allocated_size = size.value
            buffer = ct.create_string_buffer(allocated_size)
            rc = self._pdh.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE, ct.byref(size), ct.byref(count), buffer)
            if rc == _PDH_MORE_DATA:
                continue
            if rc:
                return {}, False, [f"GPU process values unavailable: 0x{rc:08X}"]
            if count.value * ct.sizeof(_PdhItem) > allocated_size:
                return {}, False, ["GPU process array failed its size check."]
            items = ct.cast(buffer, ct.POINTER(_PdhItem))
            by_pid = {}
            for index in range(count.value):
                item = items[index]
                if item.FmtValue.CStatus not in (0, 1):
                    continue
                match = _ENGINE_RE.match(item.szName or "")
                pct = _pct(item.FmtValue.doubleValue)
                if not match or pct is None:
                    continue
                pid = int(match["pid"])
                if pid <= 0:
                    continue
                key = f'luid_{match["luid"].lower()}_phys_{match["phys"]}_eng_{match["eng"]}'
                engines = by_pid.setdefault(pid, {})
                # Counter duplication is not additional workload; keep one peak.
                if key not in engines or engines[key]["pct"] < pct:
                    engines[key] = {"key": key, "name": match["type"], "pct": pct}
            processes = {}
            for pid, engines in by_pid.items():
                values = sorted(engines.values(), key=lambda engine: engine["key"])
                processes[pid] = {"gpu_pct": max(engine["pct"] for engine in values),
                                  "engines": values}
            return processes, True, []
        return {}, False, ["GPU process instances changed during collection; next sample will retry."]

    def _sample_nvml(self):
        gpus, errors = [], []
        for index, handle, name in self._nvml_devices:
            util, memory, temp = _NvmlUtilization(), _NvmlMemory(), ct.c_uint()
            urc = self._nvml.nvmlDeviceGetUtilizationRates(handle, ct.byref(util))
            mrc = self._nvml.nvmlDeviceGetMemoryInfo(handle, ct.byref(memory))
            trc = self._nvml.nvmlDeviceGetTemperature(handle, 0, ct.byref(temp))
            for metric, rc in (("utilization", urc), ("memory", mrc), ("temperature", trc)):
                if rc:
                    errors.append(f"NVIDIA GPU {index} {metric} unavailable (NVML status {rc}).")
            gpus.append({"id": f"gpu-{index}", "name": name,
                         "util_pct": _pct(util.gpu) if not urc else None,
                         "vram_used_gb": round(memory.used / _GIB, 4) if not mrc else None,
                         "vram_total_gb": round(memory.total / _GIB, 4) if not mrc else None,
                         "vram_pct": _pct(memory.used * 100 / memory.total) if not mrc and memory.total else None,
                         "temp_c": temp.value if not trc else None, "source": "NVML"})
        return gpus, errors

    def _sample_smi(self):
        if not self._smi:
            return [], ["NVIDIA device telemetry unavailable: NVML and nvidia-smi were not found."]
        try:
            result = subprocess.run(
                [self._smi, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            if result.returncode:
                return [], [f"NVIDIA telemetry unavailable (nvidia-smi exit {result.returncode})."]
            gpus = []
            for row in csv.reader(io.StringIO(result.stdout)):
                if len(row) != 6 or not row[0].strip().isdigit():
                    continue
                index, name, util, used, total, temp = [value.strip() for value in row]
                used, total = _number(used), _number(total)
                gpus.append({"id": f"gpu-{index}", "name": name, "util_pct": _pct(util),
                             "vram_used_gb": round(used / 1024, 4) if used is not None else None,
                             "vram_total_gb": round(total / 1024, 4) if total is not None else None,
                             "vram_pct": _pct(used * 100 / total) if used is not None and total else None,
                             "temp_c": _number(temp), "source": "nvidia-smi"})
            return gpus, [] if gpus else ["NVIDIA telemetry returned no usable device rows."]
        except subprocess.TimeoutExpired:
            return [], ["NVIDIA telemetry did not respond within 3 seconds."]
        except OSError:
            return [], ["NVIDIA telemetry executable could not be read."]

    def sample(self) -> dict:
        with self._lock:
            if self._closed:
                return {"gpus": [], "processes": {}, "errors": ["GPU sampler is closed."],
                        "available": False, "processes_available": False}
            processes, processes_available, errors = self._sample_engines()
            if self._nvml is not None:
                gpus, device_errors = self._sample_nvml()
            else:
                gpus, device_errors = self._sample_smi()
            errors.extend(device_errors)
            # Fixed-size per-sample diagnostics only; no accumulating error log.
            return {"gpus": gpus, "processes": processes,
                    "errors": list(dict.fromkeys(errors))[:12],
                    "available": processes_available or any(
                        gpu.get("util_pct") is not None or gpu.get("vram_used_gb") is not None for gpu in gpus),
                    "processes_available": processes_available}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._pdh is not None and self._query:
                self._pdh.PdhCloseQuery(self._query)
                self._query = ct.c_void_p()
                self._counter = ct.c_void_p()
            if self._nvml is not None:
                self._nvml.nvmlShutdown()
                self._nvml = None
                self._nvml_devices = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
