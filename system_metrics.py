"""Local network traffic, adapter identity and physical disk activity.

No speed-test downloads, remote probes, IP addresses or Wi-Fi SSIDs. Slow
Windows inventory queries run in a bounded background worker, never per sample.
"""
from __future__ import annotations

import base64
import ctypes as ct
from ctypes import wintypes as wt
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

import psutil
from gpu_metrics import _PdhItem


_INVENTORY = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$adapters=@(Get-NetAdapter -IncludeHidden -ErrorAction Stop)
$interfaces=@(Get-NetIPInterface -ErrorAction Stop)
$routes=@(Get-NetRoute -DestinationPrefix '0.0.0.0/0','::/0' -ErrorAction SilentlyContinue |
  Where-Object { $idx=$_.InterfaceIndex; @($interfaces | Where-Object { $_.InterfaceIndex -eq $idx -and $_.ConnectionState -eq 'Connected' }).Count -gt 0 } |
  Sort-Object @{Expression={ $r=$_; $ip=$interfaces | Where-Object { $_.InterfaceIndex -eq $r.InterfaceIndex -and $_.AddressFamily -eq $r.AddressFamily } | Select-Object -First 1; [int]$r.RouteMetric+[int]$ip.InterfaceMetric }})
$adapter=$null
if($routes.Count -gt 0){$adapter=$adapters | Where-Object { $_.ifIndex -eq $routes[0].InterfaceIndex -and $_.Status -eq 'Up' } | Select-Object -First 1}
$network=$null
if($null -ne $adapter){$network=@{name=$adapter.Name;medium=[string]$adapter.PhysicalMediaType;virtual=[bool]$adapter.Virtual;link_mbps=[double]$adapter.ReceiveLinkSpeed/1000000}}
$result=@{network=$network}
if(INCLUDE_HARDWARE){try{
  $memory=@(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { @{capacity=[double]$_.Capacity;type=[int]$_.SMBIOSMemoryType;part=([string]$_.PartNumber).Trim();manufacturer=([string]$_.Manufacturer).Trim();bank=[string]$_.BankLabel;slot=[string]$_.DeviceLocator;speed=[int]$_.ConfiguredClockSpeed} })
  $partitions=@(Get-Partition -ErrorAction SilentlyContinue)
  $storage=@(Get-Disk | ForEach-Object { $disk=$_; @{number=[int]$disk.Number;name=$disk.FriendlyName;size=[double]$disk.Size;bus=[string]$disk.BusType;drives=@($partitions | Where-Object { $_.DiskNumber -eq $disk.Number -and $_.DriveLetter } | ForEach-Object { [string]$_.DriveLetter+':' })} })
  $systemDisk=Get-Partition -DriveLetter $env:SystemDrive.TrimEnd(':') -ErrorAction SilentlyContinue | Get-Disk -ErrorAction SilentlyContinue | Select-Object -First 1
  $result.hardware=@{memory=$memory;storage=$storage;system_disk=if($systemDisk){@{name=$systemDisk.FriendlyName;size=[double]$systemDisk.Size;bus=[string]$systemDisk.BusType;drive=$env:SystemDrive}}else{$null}}
}catch{}}
$result | ConvertTo-Json -Depth 6 -Compress
'''


def traffic_delta(previous, current, elapsed):
    """Counter reset/interface replacement must not look like idle or a spike."""
    if previous is None or current is None or elapsed <= 0:
        return None, None
    received = current.bytes_recv - previous.bytes_recv
    sent = current.bytes_sent - previous.bytes_sent
    if received < 0 or sent < 0:
        return None, None
    return round(received * 8 / elapsed / 1_000_000, 3), round(sent * 8 / elapsed / 1_000_000, 3)


def connection_kind(adapter):
    if not adapter:
        return 'disconnected'
    if adapter.get('virtual'):
        return 'virtual'
    medium = str(adapter.get('medium', '')).lower()
    if '802.11' in medium or 'wireless' in medium:
        return 'wireless'
    if medium == '802.3':
        return 'wired'
    return 'other'


class SystemInfoSampler:
    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._metadata = None
        self._updated = 0
        self._hardware = {}
        self._previous = None
        self._thread = threading.Thread(target=self._refresh, name='network-inventory', daemon=True)
        self._thread.start()

    def _refresh(self):
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        last_hardware = 0
        while not self._stop.is_set():
            include_hardware = last_hardware == 0 or time.monotonic() - last_hardware > 60
            script = _INVENTORY.replace('INCLUDE_HARDWARE', '$true' if include_hardware else '$false')
            encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
            try:
                result = subprocess.run([str(shell), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                    capture_output=True, encoding='utf-8-sig', errors='replace', timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode != 0 or len(result.stdout) > 262144:
                    raise ValueError('Windows network inventory unavailable')
                payload = json.loads(result.stdout)
                with self._lock:
                    self._metadata = payload.get('network')
                    self._updated = time.monotonic()
                    if 'hardware' in payload:
                        self._hardware = payload['hardware']
                        last_hardware = self._updated
            except (OSError, ValueError, subprocess.TimeoutExpired):
                # Expire old metadata rather than guessing which adapter is active.
                pass
            self._stop.wait(15)

    def sample(self):
        now = time.monotonic()
        with self._lock:
            metadata, updated, hardware = self._metadata, self._updated, self._hardware
        result = {'kind': 'unknown', 'name': None, 'download_mbps': None,
                  'upload_mbps': None, 'link_mbps': None, 'status': 'starting' if not updated else 'unavailable'}
        if not updated or now - updated > 45:
            self._previous = None
            return {'network': result, 'hardware': hardware}
        result['kind'] = connection_kind(metadata)
        if not metadata:
            result['status'] = 'disconnected'
            self._previous = None
            return {'network': result, 'hardware': hardware}
        name = metadata.get('name')
        result.update(name=name, link_mbps=metadata.get('link_mbps'))
        try:
            stats = psutil.net_if_stats().get(name)
            current = psutil.net_io_counters(pernic=True, nowrap=False).get(name)
            if not stats or not stats.isup or not current:
                self._previous = None
                result.update(kind='disconnected', status='disconnected')
                return {'network': result, 'hardware': hardware}
            previous = self._previous
            if previous and previous[0] == name:
                result['download_mbps'], result['upload_mbps'] = traffic_delta(previous[2], current, now - previous[1])
            self._previous = (name, now, current)
            result['status'] = 'ok' if result['download_mbps'] is not None else 'starting'
        except (OSError, RuntimeError):
            self._previous = None
        return {'network': result, 'hardware': hardware}

    def close(self):
        self._stop.set()


class DiskActivitySampler:
    """Busiest physical drive: 100 - % Idle Time, never inferred from MiB/s."""
    def __init__(self):
        self.by_disk = {}
        self._pdh = None
        self._query, self._counter = ct.c_void_p(), ct.c_void_p()
        try:
            self._pdh = ct.WinDLL('pdh.dll')
            signatures = {
                'PdhOpenQueryW': [wt.LPCWSTR, ct.c_size_t, ct.POINTER(ct.c_void_p)],
                'PdhAddEnglishCounterW': [ct.c_void_p, wt.LPCWSTR, ct.c_size_t, ct.POINTER(ct.c_void_p)],
                'PdhCollectQueryData': [ct.c_void_p],
                'PdhGetFormattedCounterArrayW': [ct.c_void_p, ct.c_uint32, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32), ct.c_void_p],
                'PdhCloseQuery': [ct.c_void_p],
            }
            for name, args in signatures.items():
                fn = getattr(self._pdh, name)
                fn.argtypes, fn.restype = args, ct.c_uint32
            if self._pdh.PdhOpenQueryW(None, 0, ct.byref(self._query)):
                raise OSError('Disk query unavailable')
            if self._pdh.PdhAddEnglishCounterW(self._query, r'\PhysicalDisk(*)\% Idle Time', 0, ct.byref(self._counter)):
                raise OSError('Disk idle counter unavailable')
            self._pdh.PdhCollectQueryData(self._query)
        except (OSError, AttributeError):
            self.close()

    def sample(self):
        self.by_disk = {}
        if not self._pdh or not self._query:
            return None
        if self._pdh.PdhCollectQueryData(self._query):
            return None
        for _ in range(2):
            size, count = ct.c_uint32(), ct.c_uint32()
            rc = self._pdh.PdhGetFormattedCounterArrayW(self._counter, 0x200, ct.byref(size), ct.byref(count), None)
            if rc != 0x800007D2 or not 0 < size.value <= 1024 * 1024:
                return None
            allocated = size.value
            buffer = ct.create_string_buffer(allocated)
            rc = self._pdh.PdhGetFormattedCounterArrayW(self._counter, 0x200, ct.byref(size), ct.byref(count), buffer)
            if rc == 0x800007D2:
                continue
            if rc or count.value * ct.sizeof(_PdhItem) > allocated:
                return None
            items = ct.cast(buffer, ct.POINTER(_PdhItem))
            for i in range(count.value):
                item = items[i]
                identity = str(item.szName).split(' ', 1)[0]
                if identity.isdecimal() and item.FmtValue.CStatus in (0, 1) and math.isfinite(item.FmtValue.doubleValue):
                    self.by_disk[identity] = round(max(0, min(100, 100 - item.FmtValue.doubleValue)), 1)
            values = list(self.by_disk.values())
            return round(max(0, min(100, max(values))), 1) if values else None
        return None

    def close(self):
        if self._pdh and self._query:
            self._pdh.PdhCloseQuery(self._query)
        self._query = ct.c_void_p()
        self._pdh = None


class DiskTransferSampler:
    """Per-physical-drive transfer rates; warmup/reset is missing, never zero."""
    def __init__(self):
        self.previous = {}
        self.updated = time.monotonic()

    def sample(self):
        now = time.monotonic()
        elapsed = now - self.updated
        current = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        result = {}
        for name, counter in current.items():
            if not name.startswith('PhysicalDrive') or not name[13:].isdecimal():
                continue
            previous = self.previous.get(name)
            read = write = None
            if previous and elapsed > 0:
                dr, dw = counter.read_bytes - previous.read_bytes, counter.write_bytes - previous.write_bytes
                if dr >= 0 and dw >= 0:
                    read, write = round(dr / elapsed / 1024**2, 3), round(dw / elapsed / 1024**2, 3)
            result[name[13:]] = {'read_mbps': read, 'write_mbps': write}
        self.previous, self.updated = current, now
        return result
