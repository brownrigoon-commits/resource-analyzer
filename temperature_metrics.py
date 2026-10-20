"""Bundled hardware temperature reader; no control commands or public endpoint.

Only the sensor bridge runs elevated. The monitor reads a random, user-scoped,
output-only named pipe. Slow hardware queries never block resource collection.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from app_paths import bundle_dir

KINDS = ('cpu', 'ram', 'disk', 'gpu')
MAX_AGE_S = 8


def _sensor_error(payload, kind):
    errors_by_kind = payload.get('errors_by_kind')
    if isinstance(errors_by_kind, dict):
        error = errors_by_kind.get(kind)
    else:
        # Older bridges have no GPU sensors. Their CPU/DIMM prerequisite error
        # must not be presented as a missing GPU driver.
        error = payload.get('error') if kind != 'gpu' else None
    translations = {
        'PawnIO is not installed.': 'CPU·메모리 온도용 PawnIO 드라이버가 설치되지 않았습니다.',
        'Administrator permission is required for CPU and DIMM temperatures.':
            'CPU·메모리 온도를 읽으려면 센서 엔진의 관리자 승인이 필요합니다.',
    }
    return translations.get(error, error) if isinstance(error, str) else None


def summarize(payload, now=None):
    now = time.time() if now is None else now
    timestamp = payload.get('sampled_at')
    fresh = isinstance(timestamp, (float, int)) and math.isfinite(timestamp) and -2 <= now - timestamp <= MAX_AGE_S
    result = {}
    for kind in KINDS:
        sensors = []
        if fresh:
            for sensor in payload.get('sensors', []):
                value = sensor.get('temp_c')
                if sensor.get('kind') == kind and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and -20 <= value <= 150:
                    reading = {'name': str(sensor.get('name', kind))[:120], 'temp_c': round(value, 1)}
                    for field in ('source', 'hardware_id', 'device_id'):
                        if isinstance(sensor.get(field), str):
                            reading[field] = sensor[field][:512]
                    sensors.append(reading)
        status = 'ok' if sensors else ('stale' if timestamp and not fresh else 'unavailable')
        details = ' · '.join(f"{s['name']}: {s['temp_c']:g}°C" for s in sensors)
        if len(sensors) > 1:
            details = '읽을 수 있는 센서 중 최고 온도 · ' + details
        if not sensors:
            details = ('온도 수신이 지연되어 이전 값은 표시하지 않습니다.' if status == 'stale'
                       else _sensor_error(payload, kind) or '이 부품에서 읽을 수 있는 온도 센서가 없습니다.')
        result[kind] = {'temp_c': max((s['temp_c'] for s in sensors), default=None),
                        'status': status, 'details': details, 'sensors': sensors}
    return result


class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG), ('hwnd', wintypes.HWND),
                ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]


def launch_elevated(executable, arguments):
    """Use normal Windows UAC; cancellation never weakens security settings."""
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFO)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    info = SHELLEXECUTEINFO()
    info.cbSize, info.fMask, info.lpVerb = ctypes.sizeof(info), 0x40, 'runas'
    info.lpFile = str(executable)
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory, info.nShow = str(Path(executable).parent), 0
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.hProcess:
        kernel.CloseHandle(info.hProcess)


class TemperatureSampler:
    def __init__(self, autostart=True):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._payload = None
        self._error = None
        self._started = time.monotonic()
        self._thread = None
        if autostart and os.environ.get('RESOURCE_ANALYZER_DISABLE_SENSORS') != '1':
            self._thread = threading.Thread(target=self._read, name='temperature-reader', daemon=True)
            self._thread.start()

    def sample(self):
        with self._lock:
            payload, error = self._payload, self._error
        if payload is not None:
            return summarize(payload)
        status = 'starting' if not error and time.monotonic() - self._started < 90 else 'unavailable'
        return {kind: {'temp_c': None, 'status': status, 'details': error or '온도 센서 연결을 준비하고 있습니다.', 'sensors': []} for kind in KINDS}

    def _read(self):
        stream = None
        process = None
        try:
            helper = bundle_dir() / 'vendor' / 'sensors' / 'SensorBridge.exe'
            if not helper.is_file():
                raise RuntimeError('온도 센서 엔진이 포함되지 않은 빌드입니다.')
            pipe_name = 'ResourceAnalyzer.Temp.' + uuid.uuid4().hex
            arguments = ['--pipe', pipe_name, '--parent', str(os.getpid())]
            if ctypes.windll.shell32.IsUserAnAdmin():
                process = subprocess.Popen([str(helper), *arguments], creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                launch_elevated(helper, arguments)
            deadline = time.monotonic() + 90
            while not self._stop.is_set():
                try:
                    stream = open('\\\\.\\pipe\\' + pipe_name, 'rb', buffering=0)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('온도 센서 엔진 연결 시간이 초과됐습니다. 관리자 승인을 확인해 주세요.')
                    self._stop.wait(.2)
            if stream is None:
                return
            import msvcrt
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            handle = msvcrt.get_osfhandle(stream.fileno())
            pending = b''
            while not self._stop.is_set():
                available = wintypes.DWORD()
                if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                    raise RuntimeError('온도 센서 엔진 연결이 종료됐습니다.')
                if available.value:
                    pending += stream.read(min(available.value, 65536))
                    if len(pending) > 1024 * 1024:
                        raise RuntimeError('온도 센서 응답 크기를 초과했습니다.')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        payload = json.loads(line.decode('utf-8-sig'))
                        if not isinstance(payload, dict) or not isinstance(payload.get('sensors'), list):
                            raise RuntimeError('온도 센서 응답 형식을 확인할 수 없습니다.')
                        with self._lock:
                            self._payload, self._error = payload, None
                else:
                    self._stop.wait(.2)
        except Exception as error:
            with self._lock:
                self._payload = None
                self._error = ('온도 측정 관리자 승인이 취소됐습니다.' if getattr(error, 'winerror', None) == 1223 else str(error))
        finally:
            if stream is not None:
                stream.close()
            if process is not None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
