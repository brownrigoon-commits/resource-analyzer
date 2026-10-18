"""Low-overhead read-only process telemetry through one permanent PDH query.

Process V2 is preferred (Windows 11); Process is a guarded legacy fallback.
CPU is normalized to whole-PC logical CPU capacity. RAM is Working Set (GiB).
I/O is process I/O, not physical disk traffic; rates use MiB/s (2**20 bytes).
Unknown rate samples are None, including new/reassigned process instances.
"""

from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import math
import os
import re
import threading
import time


_FORMAT = 0x200 | 0x8000 | 0x1000  # DOUBLE | NOCAP100 | NOSCALE
_MORE_DATA = 0x800007D2
_COUNTERS = {
    "pid": "ID Process", "ppid": "Creating Process ID",
    "cpu": "% Processor Time", "ram": "Working Set",
    "read": "IO Read Bytes/sec", "write": "IO Write Bytes/sec",
    "elapsed": "Elapsed Time",
}


class _ValueUnion(ct.Union):
    _fields_ = [("longValue", ct.c_int32), ("doubleValue", ct.c_double),
                ("largeValue", ct.c_int64), ("stringValue", ct.c_void_p)]


class _Value(ct.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("CStatus", ct.c_uint32), ("value", _ValueUnion)]


class _Item(ct.Structure):
    _fields_ = [("szName", ct.c_wchar_p), ("FmtValue", _Value)]


class _FileTime(ct.Structure):
    _fields_ = [("low", ct.c_uint32), ("high", ct.c_uint32)]


class _RawValue(ct.Structure):
    _fields_ = [("CStatus", ct.c_uint32), ("TimeStamp", _FileTime),
                ("FirstValue", ct.c_int64), ("SecondValue", ct.c_int64),
                ("MultiCount", ct.c_uint32)]


class _RawItem(ct.Structure):
    _fields_ = [("szName", ct.c_wchar_p), ("RawValue", _RawValue)]


def _valid(value):
    return value is not None and math.isfinite(value) and value >= 0


class ProcessSampler:
    def __init__(self):
        self._lock = threading.RLock()
        self._closed = False
        self._pdh = None
        self._query = ct.c_void_p()
        self._handles = {}
        self._previous = {}
        self._births = {}
        self._last_collect = None
        self._error = None
        self.counterset = None
        self.logical_cpus = max(1, os.cpu_count() or 1)
        if os.name != "nt":
            self._error = "Windows process counters are unavailable on this platform."
            return
        try:
            self._pdh = ct.WinDLL("pdh.dll")
            signatures = {
                "PdhOpenQueryW": [wt.LPCWSTR, ct.c_size_t, ct.POINTER(ct.c_void_p)],
                "PdhAddEnglishCounterW": [ct.c_void_p, wt.LPCWSTR, ct.c_size_t, ct.POINTER(ct.c_void_p)],
                "PdhCollectQueryData": [ct.c_void_p],
                "PdhGetFormattedCounterArrayW": [ct.c_void_p, ct.c_uint32,
                    ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32), ct.c_void_p],
                "PdhGetRawCounterArrayW": [ct.c_void_p, ct.POINTER(ct.c_uint32),
                    ct.POINTER(ct.c_uint32), ct.c_void_p],
                "PdhCloseQuery": [ct.c_void_p],
            }
            for name, args in signatures.items():
                fn = getattr(self._pdh, name)
                fn.argtypes, fn.restype = args, ct.c_uint32
            # Never change ProcessNameFormat registry settings on the host.
            failures = []
            for counterset in ("Process V2", "Process"):
                try:
                    self._open(counterset)
                    self._prime()
                    self.counterset = counterset
                    break
                except (RuntimeError, OSError) as exc:
                    failures.append(str(exc))
                    self._close_query()
            else:
                self._error = "Process counters unavailable: " + "; ".join(failures)
        except (OSError, AttributeError) as exc:
            self._error = "Process counters unavailable: " + str(exc)
            self._close_query()

    def _open(self, counterset):
        rc = self._pdh.PdhOpenQueryW(None, 0, ct.byref(self._query))
        if rc:
            raise RuntimeError(f"Open {counterset}: 0x{rc:08X}")
        for key, counter in _COUNTERS.items():
            handle = ct.c_void_p()
            rc = self._pdh.PdhAddEnglishCounterW(
                self._query, f"\\{counterset}(*)\\{counter}", 0, ct.byref(handle))
            if rc:
                raise RuntimeError(f"Add {counterset}/{counter}: 0x{rc:08X}")
            self._handles[key] = handle

    def _array(self, key, raw=False):
        handle = self._handles[key]
        item_type = _RawItem if raw else _Item
        fn = self._pdh.PdhGetRawCounterArrayW if raw else self._pdh.PdhGetFormattedCounterArrayW

        def read(size, count, buffer):
            args = [handle] if raw else [handle, _FORMAT]
            return fn(*args, ct.byref(size), ct.byref(count), buffer)

        for _ in range(2):
            size, count = ct.c_uint32(), ct.c_uint32()
            rc = read(size, count, None)
            if rc == 0 and count.value == 0:
                return {}
            if rc != _MORE_DATA:
                raise RuntimeError(f"{key} array: 0x{rc:08X}")
            if not 0 < size.value <= 32 * 1024 * 1024:
                raise RuntimeError(f"{key} array exceeds safe buffer limit")
            allocated_size = size.value
            buffer = ct.create_string_buffer(allocated_size)
            rc = read(size, count, buffer)
            if rc == _MORE_DATA:
                continue
            if rc:
                raise RuntimeError(f"{key} values: 0x{rc:08X}")
            if count.value * ct.sizeof(item_type) > allocated_size:
                raise RuntimeError(f"{key} array size invalid")
            items = ct.cast(buffer, ct.POINTER(item_type))
            values, duplicates = {}, {}
            for index in range(count.value):
                item = items[index]
                name = item.szName or ""
                ordinal = duplicates.get(name, 0)
                duplicates[name] = ordinal + 1
                # Preserve invalid entries as placeholders so duplicate name
                # ordinals remain aligned across arrays on legacy providers.
                instance = (name, ordinal)
                value = item.RawValue if raw else item.FmtValue
                if value.CStatus not in (0, 1):
                    values[instance] = None
                else:
                    number = value.FirstValue if raw else value.doubleValue
                    values[instance] = number if _valid(number) else None
            return values
        raise RuntimeError(f"{key} instances changed during collection")

    def _prime(self):
        rc = self._pdh.PdhCollectQueryData(self._query)
        if rc:
            raise RuntimeError(f"Initial collect: 0x{rc:08X}")
        ids = self._array("pid")
        if not any(_valid(pid) and pid > 0 for pid in ids.values()):
            raise RuntimeError("No process instances returned")
        starts = self._array("elapsed", raw=True)
        self._previous = {key: (int(pid), starts.get(key)) for key, pid in ids.items()
                          if _valid(pid) and pid > 0 and starts.get(key) is not None}
        self._last_collect = time.monotonic()

    @staticmethod
    def _display_name(instance, pid):
        name = re.sub(r"#\d+$", "", instance[0])
        # Process V2 normally appends :PID. Match the observed PID only, so an
        # executable ending with an unrelated number is never truncated.
        for suffix in (f":{pid}", f"_{pid}", f" ({pid})"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        return name

    def sample(self) -> dict:
        with self._lock:
            if self._closed:
                return {"available": False, "rows": [], "errors": ["Process sampler is closed."]}
            if self._error or not self._query:
                return {"available": False, "rows": [], "errors": [self._error or "Process counters unavailable."]}
            if self._last_collect is not None and time.monotonic() - self._last_collect < 0.05:
                return {"available": False, "rows": [], "errors": ["Process counters are warming up; wait for the next sample."]}
            try:
                rc = self._pdh.PdhCollectQueryData(self._query)
                if rc:
                    raise RuntimeError(f"Process collection unavailable: 0x{rc:08X}")
                self._last_collect = time.monotonic()
                # All arrays refer to this single collected PDH sample. Read PID
                # before and after the other arrays as an additional join guard.
                ids_before = self._array("pid")
                values = {key: self._array(key) for key in _COUNTERS if key != "pid"}
                starts = self._array("elapsed", raw=True)
                ids_after = self._array("pid")
                now = time.time()
                rows, current, births, seen = [], {}, {}, set()
                for instance, pid_value in ids_before.items():
                    if not _valid(pid_value) or pid_value <= 0 or pid_value != ids_after.get(instance):
                        continue
                    pid = int(pid_value)
                    name = self._display_name(instance, pid)
                    if name.casefold() in ("_total", "idle") or pid in seen:
                        continue
                    seen.add(pid)
                    start = starts.get(instance)
                    identity = str(start) if start is not None else None
                    generation = (pid, start)
                    current[instance] = generation
                    rate_valid = start is not None and self._previous.get(instance) == generation
                    elapsed = values["elapsed"].get(instance)
                    created_at = self._births.get(generation)
                    if created_at is None and _valid(elapsed):
                        created_at = now - elapsed
                    if created_at is not None:
                        births[generation] = created_at

                    def value(key, divisor=1.0, rate=False):
                        number = values[key].get(instance)
                        if not _valid(number) or (rate and not rate_valid):
                            return None
                        return number / divisor

                    cpu = value("cpu", self.logical_cpus, rate=True)
                    parent = value("ppid")
                    rows.append({"pid": pid, "ppid": int(parent) if parent is not None else None,
                                 "name": name,
                                 "cpu_pct": min(100.0, cpu) if cpu is not None else None,
                                 "ram_gb": value("ram", 1024 ** 3),
                                 "io_read_mbps": value("read", 1024 ** 2, rate=True),
                                 "io_write_mbps": value("write", 1024 ** 2, rate=True),
                                 "created_at": created_at, "identity": identity})
                self._previous = current
                self._births = births  # Remove departed instances; no unbounded history.
                return {"available": True, "rows": rows, "errors": []}
            except (OSError, RuntimeError) as exc:
                # No stale rates across a failed collection boundary.
                self._previous = {}
                return {"available": False, "rows": [], "errors": [str(exc)]}

    def _close_query(self):
        if self._pdh is not None and self._query:
            self._pdh.PdhCloseQuery(self._query)
        self._query = ct.c_void_p()
        self._handles = {}
        self._previous = {}
        self._births = {}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_query()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
