"""Read-only Windows GPU telemetry; no model imports or device configuration.

PDH engine data measures the interval between calls. NVML device utilization uses
the driver's own recent sampling window, so those two percentages need not agree.
NVIDIA GPU indexes are NVIDIA enumeration indexes, not Task Manager indexes.
Windows adapters use DXGI LUID identities and support every WDDM GPU vendor.
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
import shutil
import subprocess
import threading
import time

from windows_gpu import (aggregate_engines, aggregate_memory, enumerate_adapters,
                         sample_adapter_temperatures)


_GIB = 1024 ** 3
_PDH_MORE_DATA = 0x800007D2
_PDH_FMT_DOUBLE = 0x00000200


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
        self._memory_counter = ct.c_void_p()
        self._shared_counter = ct.c_void_p()
        self._pdh = None
        self._pdh_error = None
        self._counter_errors = {}
        self._pdh_last_collect = None
        self._pdh_collected = False
        self._windows_utilization = {}
        self._windows_adapters = []
        self._windows_errors = []
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
        self._windows_adapters, self._windows_errors = enumerate_adapters()
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
            for name, path, counter in (
                ("engine", r"\GPU Engine(*)\Utilization Percentage", self._counter),
                ("dedicated memory", r"\GPU Adapter Memory(*)\Dedicated Usage", self._memory_counter),
                ("shared memory", r"\GPU Adapter Memory(*)\Shared Usage", self._shared_counter),
            ):
                rc = pdh.PdhAddEnglishCounterW(self._query, path, 0, ct.byref(counter))
                if rc:
                    self._counter_errors[name] = f"GPU {name} counter unavailable: 0x{rc:08X}"
                    counter.value = None
            if not any((self._counter, self._memory_counter, self._shared_counter)):
                raise RuntimeError("No GPU performance counters are available.")
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

    def _read_counter(self, counter, label):
        if not counter:
            return [], False, [self._counter_errors.get(label, f"GPU {label} counter unavailable.")]
        # Query twice per the documented buffer-size contract; retry at most once
        # if process churn increased the required array size between these calls.
        for _ in range(2):
            size, count = ct.c_uint32(), ct.c_uint32()
            rc = self._pdh.PdhGetFormattedCounterArrayW(
                counter, _PDH_FMT_DOUBLE, ct.byref(size), ct.byref(count), None)
            if rc == 0 and count.value == 0:
                return [], True, []
            if rc != _PDH_MORE_DATA:
                return [], False, [f"GPU {label} array unavailable: 0x{rc:08X}"]
            if not 0 < size.value <= 32 * 1024 * 1024:
                return [], False, [f"GPU {label} array exceeds the safe buffer limit."]
            allocated_size = size.value
            buffer = ct.create_string_buffer(allocated_size)
            rc = self._pdh.PdhGetFormattedCounterArrayW(
                counter, _PDH_FMT_DOUBLE, ct.byref(size), ct.byref(count), buffer)
            if rc == _PDH_MORE_DATA:
                continue
            if rc:
                return [], False, [f"GPU {label} values unavailable: 0x{rc:08X}"]
            if count.value * ct.sizeof(_PdhItem) > allocated_size:
                return [], False, [f"GPU {label} array failed its size check."]
            items = ct.cast(buffer, ct.POINTER(_PdhItem))
            rows = []
            for index in range(count.value):
                item = items[index]
                if item.FmtValue.CStatus not in (0, 1):
                    continue
                value = _number(item.FmtValue.doubleValue)
                if value is not None:
                    rows.append((item.szName or "", value))
            if count.value and not rows:
                return [], False, [f"GPU {label} counter has no valid samples yet."]
            return rows, True, []
        return [], False, [f"GPU {label} instances changed during collection; next sample will retry."]

    def _sample_engines(self):
        # Never reuse utilization or memory after a failed or premature collect.
        self._windows_utilization = {}
        self._pdh_collected = False
        if not self._pdh or not self._query:
            return {}, False, [self._pdh_error or "GPU process counters unavailable."]
        now = time.monotonic()
        if self._pdh_last_collect is not None and now - self._pdh_last_collect < 0.1:
            return {}, False, ["GPU process counters are warming up; wait for the next sample."]
        rc = self._pdh.PdhCollectQueryData(self._query)
        if rc:
            # A rate counter needs two successful collections after an error.
            self._pdh_last_collect = None
            return {}, False, [f"GPU process collection unavailable: 0x{rc:08X}"]
        previous = self._pdh_last_collect
        self._pdh_last_collect = now
        self._pdh_collected = True
        if previous is None:
            return {}, False, ["GPU process counters are warming up; wait for the next sample."]
        rows, available, errors = self._read_counter(self._counter, "engine")
        processes, self._windows_utilization = aggregate_engines(rows)
        return processes, available, errors

    def _sample_windows(self, nvidia_present=False):
        # NVML/SMI indexes cannot be matched to DXGI by order or product name.
        # Keep their NVIDIA rows, and independently add every other vendor.
        adapters = [adapter for adapter in self._windows_adapters
                    if not nvidia_present or adapter["vendor_id"] != 0x10DE]
        errors = list(self._windows_errors)
        if not adapters:
            return [], errors
        temperatures, temperature_errors = sample_adapter_temperatures(adapters)
        errors.extend(temperature_errors)
        dedicated, shared = {}, {}
        if self._pdh_collected:
            rows, _, counter_errors = self._read_counter(self._memory_counter, "dedicated memory")
            dedicated = aggregate_memory(rows)
            errors.extend(counter_errors)
            rows, _, counter_errors = self._read_counter(self._shared_counter, "shared memory")
            shared = aggregate_memory(rows)
            errors.extend(counter_errors)
        gpus = []
        for adapter in adapters:
            luid = adapter["adapter_luid"]
            used = dedicated.get(luid)
            total = adapter["dedicated_bytes"]
            shared_used = shared.get(luid)
            gpus.append({
                "id": adapter["id"], "name": adapter["name"],
                "adapter_luid": luid, "vendor_id": adapter["vendor_id"],
                "util_pct": self._windows_utilization.get(luid),
                "vram_used_gb": round(used / _GIB, 4) if used is not None else None,
                "vram_total_gb": round(total / _GIB, 4),
                "vram_pct": _pct(used * 100 / total) if used is not None and total else None,
                "shared_used_gb": round(shared_used / _GIB, 4) if shared_used is not None else None,
                "shared_total_gb": round(adapter["shared_bytes"] / _GIB, 4),
                "temp_c": temperatures.get(luid),
                "temp_source": "Windows D3DKMT" if luid in temperatures else None,
                "source": "Windows DXGI/PDH",
            })
        return gpus, errors

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
            windows_gpus, windows_errors = self._sample_windows()
            windows_nvidia = [gpu for gpu in windows_gpus if gpu["vendor_id"] == 0x10DE]
            windows_nvidia_count = len(windows_nvidia)
            windows_metrics_complete = bool(windows_nvidia) and all(
                gpu.get("util_pct") is not None and gpu.get("vram_used_gb") is not None
                for gpu in windows_nvidia)
            native_metrics_missing = any(gpu.get("util_pct") is None
                                         or gpu.get("vram_used_gb") is None for gpu in gpus)
            if gpus and (windows_nvidia_count > len(gpus)
                         or (windows_nvidia_count == len(gpus) and native_metrics_missing
                             and windows_metrics_complete)):
                # Partial NVML coverage must not hide valid Windows telemetry.
                # Select the complete Windows list, without guessing which native
                # index belongs to which LUID. Extra TCC-only NVML devices remain
                # supported when Windows enumerates fewer devices than NVML. An
                # equal-size metric fallback must have valid replacement values
                # for every card, so PDH warmup/failure never discards native data.
                reason = ("NVIDIA device telemetry was incomplete; using Windows GPU counters."
                          if windows_metrics_complete else
                          "NVIDIA enumeration was incomplete; using Windows device inventory.")
                gpus = []
                errors.append(reason)
            elif gpus:
                windows_gpus = [gpu for gpu in windows_gpus if gpu["vendor_id"] != 0x10DE]
            # Missing NVIDIA libraries are irrelevant on an AMD/Intel-only PC.
            if gpus or not windows_gpus:
                errors.extend(device_errors)
            gpus.extend(windows_gpus)
            errors.extend(windows_errors)
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
                self._memory_counter = ct.c_void_p()
                self._shared_counter = ct.c_void_p()
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
