"""Small cooperative governor client, copied alongside a supported local worker.

Only use a session inside the process which performs the synchronous heavy
work. checkpoint() belongs between clips/jobs; it never changes a live model's
device. A missing governor is an explicit failure after a bounded wait.
Before normal exit, synchronize device work and dispose of model references.
On an exception the worker must exit: its traceback can retain model memory,
so the lease remains reserved until the governor verifies process termination.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


_DEVICES = {'whisper-asr': {'cpu', 'cuda'}, 'cosyvoice-synthesis': {'cuda'},
            'audio-preprocess': {'cpu'}, 'qwen-vision': {'cpu', 'cuda', 'hybrid'},
            'qwen-summary': {'cpu', 'cuda'}}
_RESPONSE_LIMIT = 64 * 1024


class _TemporaryFailure(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # A local policy request must never forward worker identity or its
        # control token to another origin, including a redirected web server.
        return None


def _created_at():
    try:
        import psutil
        return psutil.Process(os.getpid()).create_time()
    except ImportError:
        if os.name != 'nt':
            raise RuntimeError('프로세스 생성 시각 확인에 psutil이 필요합니다.') from None
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), ctypes.byref(created),
                                      ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise RuntimeError('작업 프로세스 생성 시각을 확인할 수 없습니다.')
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600


class ResourceSession:
    def __init__(self, stage, url='http://127.0.0.1:7442', *, max_wait_s=180,
                 request_timeout_s=3, retry_interval_s=1):
        if not isinstance(stage, str) or stage not in _DEVICES:
            raise ValueError('지원되는 중앙 정책 작업 단계가 아닙니다.')
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError('유효한 로컬 분석기 주소가 필요합니다.') from None
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
                or parsed.username or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or port is None):
            raise ValueError('주소는 포트가 있는 localhost 또는 127.0.0.1 HTTP 주소여야 합니다.')
        for value in (max_wait_s, request_timeout_s, retry_interval_s):
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise ValueError('대기 시간과 요청 제한은 양수여야 합니다.')
        self.stage, self.url = stage, url.rstrip('/')
        self.max_wait_s = min(float(max_wait_s), 180)
        self.request_timeout_s = min(float(request_timeout_s), 10)
        self.retry_interval_s = min(float(retry_interval_s), 5)
        self.device = None
        self.threads = None
        self.lease_id = None
        self._payload = None
        self._token = None
        self._entered = False
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _json(self, request):
        try:
            remaining = getattr(self, '_operation_deadline', float('inf')) - time.monotonic()
            if remaining <= 0:
                raise _TemporaryFailure('중앙 정책 요청 제한 시간이 끝났습니다.')
            with self._opener.open(request, timeout=min(self.request_timeout_s, remaining)) as response:
                data = response.read(_RESPONSE_LIMIT + 1)
            if len(data) > _RESPONSE_LIMIT:
                raise RuntimeError('중앙 정책 응답이 허용 크기를 넘었습니다.')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise RuntimeError('중앙 정책이 올바른 JSON 객체를 반환하지 않았습니다.')
            return value
        except HTTPError:
            raise
        except (OSError, URLError, TimeoutError) as error:
            raise _TemporaryFailure('로컬 중앙 정책에 연결할 수 없습니다.') from error
        except (ValueError, UnicodeError) as error:
            raise RuntimeError('중앙 정책 응답을 해석할 수 없습니다.') from error

    def _health(self):
        result = self._json(Request(self.url + '/api/health', headers={'Accept': 'application/json'}))
        token = result.get('csrf_token')
        if (result.get('app_id') != 'resource-analyzer' or not isinstance(token, str)
                or not 24 <= len(token) <= 256):
            raise RuntimeError('연결된 서버가 올바른 자원 분석기인지 확인할 수 없습니다.')
        self._token = token

    def _post(self, operation, payload):
        try:
            if self._token is None:
                self._health()
            for attempt in range(2):
                request = Request(self.url + '/api/governor/' + operation,
                                  data=json.dumps(payload, allow_nan=False).encode('utf-8'),
                                  headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                                           'X-Resource-Analyzer-Token': self._token}, method='POST')
                try:
                    return self._json(request)
                except HTTPError as error:
                    if error.code == 403 and attempt == 0:
                        error.close()
                        self._token = None
                        self._health()
                        continue
                    if error.code in (429, 500, 502, 503, 504):
                        error.close()
                        raise _TemporaryFailure('중앙 정책 서버가 일시적으로 응답하지 않습니다.') from error
                    error.close()
                    raise RuntimeError('중앙 정책이 요청을 거부했습니다 (HTTP %s).' % error.code) from error
        except HTTPError as error:
            error.close()
            if error.code in (429, 500, 502, 503, 504):
                raise _TemporaryFailure('중앙 정책 상태를 확인할 수 없습니다.') from error
            raise RuntimeError('중앙 정책 상태 요청이 거부되었습니다 (HTTP %s).' % error.code) from error
        raise RuntimeError('중앙 정책 인증을 갱신하지 못했습니다.')

    def _wait(self, operation, payload):
        deadline = time.monotonic() + self.max_wait_s
        self._operation_deadline = deadline
        reason = '중앙 정책의 허가를 기다리고 있습니다.'
        while True:
            retry = self.retry_interval_s
            try:
                response = self._post(operation, payload)
                if operation == 'release' and response.get('released') is True:
                    return response
                decision = response.get('decision')
                if decision in ('cpu', 'cuda') and operation != 'release':
                    threads, token = response.get('threads'), response.get('lease_id')
                    if (decision not in _DEVICES[self.stage] or not isinstance(threads, int)
                            or isinstance(threads, bool) or not 1 <= threads <= 8
                            or not isinstance(token, str) or not 24 <= len(token) <= 128):
                        raise RuntimeError('중앙 정책이 유효하지 않은 장치 또는 임대를 반환했습니다.')
                    if self.lease_id is not None and (token != self.lease_id or decision != self.device):
                        raise RuntimeError('실행 중인 작업의 장치 또는 임대를 변경할 수 없습니다.')
                    return response
                if decision != 'wait':
                    raise RuntimeError('중앙 정책이 알 수 없는 실행 결정을 반환했습니다.')
                text = response.get('reason')
                if isinstance(text, str):
                    reason = text[:300]
                suggested = response.get('retry_after_s')
                if (isinstance(suggested, (int, float)) and not isinstance(suggested, bool)
                        and math.isfinite(suggested) and suggested > 0):
                    retry = min(max(float(suggested), self.retry_interval_s), 5)
            except _TemporaryFailure as error:
                reason = str(error)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('중앙 정책 허가를 %g초 안에 받지 못했습니다. 작업을 시작하지 않습니다. %s'
                                   % (self.max_wait_s, reason))
            time.sleep(min(retry, remaining))

    def __enter__(self):
        if self._entered or self._payload is not None:
            raise RuntimeError('작업 세션은 한 번만 사용할 수 있습니다.')
        self._payload = {'stage': self.stage, 'owner_pid': os.getpid(),
                         'owner_created': _created_at(), 'request_id': uuid.uuid4().hex}
        result = self._wait('acquire', self._payload)
        self.device, self.threads, self.lease_id = result['decision'], result['threads'], result['lease_id']
        self._entered = True
        return self

    def checkpoint(self):
        if not self._entered:
            raise RuntimeError('실행 중인 작업 세션이 없습니다.')
        self._wait('checkpoint', {**self._payload, 'lease_id': self.lease_id})
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._entered:
            return False
        if exc_type is not None:
            self._entered = False
            if exc_value is not None and hasattr(exc_value, 'add_note'):
                exc_value.add_note('작업 실패로 임대를 유지합니다. 이 작업 프로세스가 종료되어야 다음 무거운 작업을 허용합니다.')
            return False
        try:
            self._wait('release', {**self._payload, 'lease_id': self.lease_id,
                                   'completed': True})
        finally:
            self._entered = False
        return False


class FileResourceSession(ResourceSession):
    """The same job contract over private files, with no HTTP/socket calls."""
    def __init__(self, stage, data_dir=None, max_wait_s=180, *, retry_interval_s=1):
        import resource_ipc
        super().__init__(stage, max_wait_s=max_wait_s, retry_interval_s=retry_interval_s)
        self.data_dir = resource_ipc.default_data_dir() if data_dir is None else data_dir

    def _post(self, operation, payload):
        import resource_ipc as ipc
        if operation not in ipc.OPERATIONS:
            raise RuntimeError('지원되는 파일 IPC 작업이 아닙니다.')
        try:
            folder = ipc.mailbox(self.data_dir)
            metadata = ipc.read_metadata(folder)
            if not ipc.server_alive(metadata):
                raise _TemporaryFailure('로컬 파일 IPC 서버의 실행을 확인할 수 없습니다.')
        except OSError as error:
            raise _TemporaryFailure('로컬 파일 IPC 서버에 연결할 수 없습니다.') from error
        except (ValueError, TypeError) as error:
            raise RuntimeError('로컬 파일 IPC 메타데이터 검증에 실패했습니다.') from error
        token = metadata['token']
        ident, nonce = uuid.uuid4().hex, secrets.token_hex(32)
        request_path = folder / (ident + '.requests.json')
        response_path = folder / (ident + '.response.json')
        request = ipc.envelope(token, {'protocol': ipc.PROTOCOL, 'id': ident, 'nonce': nonce,
                                      'operation': operation, 'payload': payload})
        deadline = min(getattr(self, '_operation_deadline', time.monotonic() + self.max_wait_s),
                       time.monotonic() + self.request_timeout_s)
        published = False
        try:
            ipc._atomic_json(request_path, request, ipc.REQUEST_LIMIT)
            published = True
            next_metadata_check = time.monotonic() + 1
            while True:
                try:
                    # Missing responses are the common case. Check one name;
                    # perform the full parent/file/HMAC validation only once
                    # there is a response, rather than walking AppData at 20Hz.
                    response_path.lstat()
                except FileNotFoundError:
                    result = None
                else:
                    result = ipc._read_json(response_path, ipc.RESPONSE_LIMIT)
                if result is not None:
                    if (set(result) not in ({'protocol', 'id', 'nonce', 'ok', 'result', 'signature'},
                                            {'protocol', 'id', 'nonce', 'ok', 'error', 'message', 'signature'})
                            or result.get('protocol') != ipc.PROTOCOL or result.get('id') != ident
                            or result.get('nonce') != nonce or not ipc.authenticated(token, result)):
                        raise RuntimeError('파일 IPC 응답 서명 또는 요청 nonce가 일치하지 않습니다.')
                    if result.get('ok') is True and isinstance(result.get('result'), dict):
                        return result['result']
                    if result.get('ok') is False:
                        message = result.get('message')
                        raise RuntimeError(message[:300] if isinstance(message, str)
                                           else '중앙 정책이 파일 IPC 요청을 거부했습니다.')
                    raise RuntimeError('파일 IPC 응답 형식이 유효하지 않습니다.')
                if time.monotonic() >= next_metadata_check:
                    current = ipc.read_metadata(folder)
                    if (current['token'] != token or current['server_pid'] != metadata['server_pid']
                            or current['server_created'] != metadata['server_created']):
                        raise _TemporaryFailure('로컬 파일 IPC 서버가 다시 시작되어 재연결합니다.')
                    if not ipc.server_alive(current):
                        raise _TemporaryFailure('로컬 파일 IPC 서버의 실행을 확인할 수 없습니다.')
                    next_metadata_check = time.monotonic() + 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _TemporaryFailure('로컬 파일 IPC 응답을 기다리고 있습니다.')
                time.sleep(min(.05, remaining))
        except FileExistsError as error:
            raise _TemporaryFailure('파일 IPC 요청 이름이 사용 중이어서 다시 시도합니다.') from error
        except OSError as error:
            raise _TemporaryFailure('로컬 파일 IPC 파일에 접근할 수 없습니다.') from error
        except (ValueError, TypeError, OverflowError) as error:
            raise RuntimeError('로컬 파일 IPC 메시지 검증에 실패했습니다.') from error
        finally:
            # Only the random names generated by this call may be cleaned up.
            # If exclusive publication failed, neither name belongs to us.
            if published:
                for path in (request_path, response_path):
                    try:
                        ipc._remove_regular(path)
                    except OSError:
                        pass
