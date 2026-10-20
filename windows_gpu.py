"""Vendor-independent, read-only Windows GPU identity and counter aggregation.

DXGI supplies the adapter LUID, name and memory capacity. PDH instances are
matched to that exact LUID; enumeration order and similar product names are
never used to associate a counter with a card.
"""

from __future__ import annotations

import ctypes as ct
import math
import os
from pathlib import Path
import re
import uuid


_ENGINE_RE = re.compile(
    r"^pid_(?P<pid>\d+)_luid_(?P<high>0x[0-9a-f]+)_(?P<low>0x[0-9a-f]+)"
    r"_phys_(?P<phys>\d+)_eng_(?P<eng>\d+)_engtype_(?P<type>.+?)(?:#\d+)?$",
    re.IGNORECASE,
)
_MEMORY_RE = re.compile(
    r"^luid_(?P<high>0x[0-9a-f]+)_(?P<low>0x[0-9a-f]+)"
    r"_phys_(?P<phys>\d+)(?:#\d+)?$",
    re.IGNORECASE,
)


class _Guid(ct.Structure):
    _fields_ = [("data1", ct.c_uint32), ("data2", ct.c_uint16),
                ("data3", ct.c_uint16), ("data4", ct.c_ubyte * 8)]


class _Luid(ct.Structure):
    _fields_ = [("low", ct.c_uint32), ("high", ct.c_int32)]


class _AdapterDesc1(ct.Structure):
    _fields_ = [
        ("description", ct.c_wchar * 128),
        ("vendor_id", ct.c_uint32), ("device_id", ct.c_uint32),
        ("subsystem_id", ct.c_uint32), ("revision", ct.c_uint32),
        ("dedicated_video_memory", ct.c_size_t),
        ("dedicated_system_memory", ct.c_size_t),
        ("shared_system_memory", ct.c_size_t),
        ("luid", _Luid), ("flags", ct.c_uint32),
    ]


def luid_key(high: int, low: int) -> str:
    return f"0x{high & 0xffffffff:08x}_0x{low & 0xffffffff:08x}"


def _instance_luid(match):
    return luid_key(int(match["high"], 16), int(match["low"], 16))


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _com_method(pointer, slot, result_type, *argument_types):
    vtable = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    return ct.WINFUNCTYPE(result_type, ct.c_void_p, *argument_types)(vtable[slot])


def enumerate_adapters():
    """Return physical/hardware DXGI adapters, releasing every COM reference.

    IDXGIFactory1::EnumAdapters1 and IDXGIAdapter1::GetDesc1 are inherited COM
    slots 12 and 10 respectively. The software adapter flag excludes WARP.
    https://learn.microsoft.com/windows/win32/api/dxgi/ns-dxgi-dxgi_adapter_desc1
    """
    if os.name != "nt":
        return [], ["Windows DXGI adapters are unavailable on this platform."]
    factory = ct.c_void_p()
    adapters, errors = [], []
    try:
        dll_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "dxgi.dll"
        dxgi = ct.WinDLL(str(dll_path))
        create = dxgi.CreateDXGIFactory1
        create.argtypes = [ct.POINTER(_Guid), ct.POINTER(ct.c_void_p)]
        create.restype = ct.c_int32
        iid = _Guid.from_buffer_copy(uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
        status = create(ct.byref(iid), ct.byref(factory))
        if status < 0 or not factory:
            raise RuntimeError(f"CreateDXGIFactory1: 0x{status & 0xffffffff:08X}")
        enumerate_one = _com_method(factory, 12, ct.c_int32,
                                    ct.c_uint32, ct.POINTER(ct.c_void_p))
        for index in range(64):
            adapter = ct.c_void_p()
            try:
                status = enumerate_one(factory, index, ct.byref(adapter))
                if status & 0xffffffff == 0x887A0002:  # DXGI_ERROR_NOT_FOUND
                    break
                if status < 0 or not adapter:
                    raise RuntimeError(f"EnumAdapters1: 0x{status & 0xffffffff:08X}")
                description = _AdapterDesc1()
                get_description = _com_method(adapter, 10, ct.c_int32,
                                              ct.POINTER(_AdapterDesc1))
                status = get_description(adapter, ct.byref(description))
                if status < 0:
                    errors.append(f"DXGI adapter {index} description unavailable: 0x{status & 0xffffffff:08X}")
                    continue
                # Windows can also expose an unflagged Basic Render adapter.
                # Microsoft documents its exact vendor/device pair; filtering
                # by that identity avoids excluding real unknown-vendor cards.
                # https://learn.microsoft.com/windows/win32/direct3ddxgi/d3d10-graphics-programming-guide-dxgi
                if (description.flags & 2
                        or (description.vendor_id, description.device_id) == (0x1414, 0x8C)):
                    continue
                luid = luid_key(description.luid.high, description.luid.low)
                if any(adapter["adapter_luid"] == luid for adapter in adapters):
                    continue
                adapters.append({
                    "id": f"gpu-luid-{luid}",
                    "adapter_luid": luid,
                    "name": description.description or f"Windows GPU {index}",
                    "vendor_id": description.vendor_id,
                    # Firmware-reserved system memory is also dedicated GPU
                    # memory on integrated adapters; shared capacity is separate.
                    "dedicated_bytes": (description.dedicated_video_memory
                                        + description.dedicated_system_memory),
                    "shared_bytes": description.shared_system_memory,
                })
            finally:
                if adapter:
                    _com_method(adapter, 2, ct.c_uint32)(adapter)
        else:
            errors.append("DXGI adapter enumeration reached its safety limit.")
    except (OSError, AttributeError, RuntimeError, ValueError) as exc:
        errors.append("Windows GPU adapter enumeration unavailable: " + str(exc))
    finally:
        if factory:
            _com_method(factory, 2, ct.c_uint32)(factory)
    return adapters, errors


def aggregate_engines(rows):
    """Sum process time on each engine, then choose each adapter's busiest one.

    PID 0 can contribute to device utilization but is not a process-table row.
    Duplicate counter instances do not represent additional work. Missing data
    produces no entry (not an invented zero). See Microsoft's aggregation:
    https://devblogs.microsoft.com/directx/gpus-in-the-task-manager/
    """
    by_pid = {}
    for name, value in rows:
        match = _ENGINE_RE.match(name or "")
        value = _number(value)
        if not match or value is None:
            continue
        luid = _instance_luid(match)
        pid = int(match["pid"])
        key = f'luid_{luid}_phys_{int(match["phys"])}_eng_{int(match["eng"])}'
        engines = by_pid.setdefault(pid, {})
        value = min(100.0, value)
        if key not in engines or engines[key]["pct"] < value:
            engines[key] = {"key": key, "name": match["type"],
                            "pct": value, "adapter_luid": luid}
    adapter_engines = {}
    processes = {}
    for pid, engines in by_pid.items():
        for engine in engines.values():
            totals = adapter_engines.setdefault(engine["adapter_luid"], {})
            totals[engine["key"]] = totals.get(engine["key"], 0.0) + engine["pct"]
        if pid > 0:
            values = [{"key": engine["key"], "name": engine["name"],
                       "pct": round(engine["pct"], 3)}
                      for engine in sorted(engines.values(), key=lambda item: item["key"])]
            processes[pid] = {"gpu_pct": max(engine["pct"] for engine in values),
                              "engines": values}
    utilization = {luid: round(min(100.0, max(engines.values())), 3)
                   for luid, engines in adapter_engines.items()}
    return processes, utilization


def aggregate_memory(rows):
    """Read adapter memory directly, never sum the shared per-process copies."""
    physical = {}
    for name, value in rows:
        match = _MEMORY_RE.match(name or "")
        value = _number(value)
        if not match or value is None:
            continue
        luid = _instance_luid(match)
        key = (luid, int(match["phys"]))
        physical[key] = max(physical.get(key, 0.0), value)
    totals = {}
    for (luid, _), value in physical.items():
        totals[luid] = totals.get(luid, 0.0) + value
    return totals
