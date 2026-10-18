"""Bounded, authenticated local file IPC for workers with sockets disabled.

The mailbox inherits the user's existing private application-data ACL. It only
contains fixed metadata and random request/response names, never paths to run,
commands, model files, or audio. No socket or network service is used here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

import psutil


APP_ID = 'resource-analyzer'
PROTOCOL = 1
REQUEST_LIMIT = 4096
RESPONSE_LIMIT = 64 * 1024
METADATA_LIMIT = 4096
OPERATIONS = frozenset(('acquire', 'checkpoint', 'release'))
_REQUEST_NAME = re.compile(r'([0-9a-f]{32})\.requests\.json\Z')
_NONCE = re.compile(r'[0-9a-f]{64}\Z')
_SIGNATURE = re.compile(r'[0-9a-f]{64}\Z')
_REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def default_data_dir():
    local = os.environ.get('LOCALAPPDATA')
    return (Path(local) if local else Path.home() / 'AppData' / 'Local') / 'ResourceAnalyzer'


def _plain(info, directory=False):
    return (not info.st_file_attributes & _REPARSE_POINT if hasattr(info, 'st_file_attributes') else True) and (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def _plain_directories(folder):
    for component in reversed((folder, *folder.parents)):
        if not _plain(component.lstat(), directory=True):
            raise ValueError('IPC 경로에 링크 또는 일반 폴더가 아닌 항목이 있습니다.')


def mailbox(data_dir, create=False):
    root = Path(data_dir).expanduser().absolute()
    # Reject junctions as well as symlinks, including those in the parent path.
    _plain_directories(root)
    folder = root / 'ipc'
    if create:
        # Inherit existing AppData permissions; never broaden an ACL.
        folder.mkdir(mode=0o700, exist_ok=True)
    if not _plain(folder.lstat(), directory=True):
        raise ValueError('IPC 폴더에 링크를 사용할 수 없습니다.')
    return folder


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('중복 JSON 필드를 사용할 수 없습니다.')
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError('유한한 JSON 숫자만 허용합니다.')


def _read_json(path, limit):
    path = Path(path)
    _plain_directories(path.parent)
    before = path.lstat()
    if not _plain(before) or before.st_size > limit:
        raise ValueError('IPC 파일이 일반 파일이 아니거나 크기 제한을 넘었습니다.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if (not _plain(opened) or opened.st_size > limit
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError('읽는 동안 IPC 파일이 변경되었습니다.')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('IPC 파일 크기 제한을 넘었습니다.')
    value = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('IPC 메시지는 JSON 객체여야 합니다.')
    return value


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _atomic_json(path, value, limit, replace=False):
    """Publish only complete files; random messages never overwrite a name."""
    path = Path(path)
    data = _encoded(value)
    if len(data) > limit:
        raise ValueError('IPC 메시지 크기 제한을 넘었습니다.')
    _plain_directories(path.parent)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        elif os.name == 'nt':
            # Windows rename is atomic and refuses an existing destination.
            # Avoid a second hard-link name which could be held open by a
            # reader just as we try to remove the temporary name.
            os.rename(temporary, path)
        else:
            # A hard link publishes a complete file atomically with exclusive
            # destination semantics on NTFS and normal POSIX filesystems.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def envelope(token, body):
    return {**body, 'signature': hmac.new(token.encode('ascii'), _encoded(body), hashlib.sha256).hexdigest()}


def authenticated(token, value):
    if not isinstance(value, dict):
        return False
    signature = value.get('signature')
    if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        return False
    body = {key: item for key, item in value.items() if key != 'signature'}
    expected = hmac.new(token.encode('ascii'), _encoded(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def read_metadata(folder):
    value = _read_json(Path(folder) / 'server.json', METADATA_LIMIT)
    token, pid, created = (value.get(key) for key in ('token', 'server_pid', 'server_created'))
    if (value.get('app_id') != APP_ID or value.get('protocol') != PROTOCOL
            or not isinstance(value.get('version'), str) or not 1 <= len(value['version']) <= 128
            or not isinstance(token, str) or not 24 <= len(token) <= 256 or not token.isascii()
            or not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 0xffffffff
            or not isinstance(created, (int, float)) or isinstance(created, bool)):
        raise ValueError('로컬 중앙 정책 메타데이터를 확인할 수 없습니다.')
    try:
        if not math.isfinite(created) or created <= 0:
            raise ValueError('중앙 정책 프로세스 생성 시각이 유효하지 않습니다.')
    except OverflowError:
        raise ValueError('중앙 정책 프로세스 생성 시각이 유효하지 않습니다.') from None
    return value


def server_alive(metadata):
    try:
        process = psutil.Process(metadata['server_pid'])
        return (abs(process.create_time() - metadata['server_created']) <= .001
                and process.is_running() and process.status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError) as error:
        raise PermissionError('IPC 서버 프로세스의 소유권을 확인할 수 없습니다.') from error


def _remove_regular(path):
    try:
        _plain_directories(Path(path).parent)
        if _plain(Path(path).lstat()):
            Path(path).unlink()
    except (FileNotFoundError, ValueError):
        pass


class LocalIPC:
    def __init__(self, governor, data_dir, token, version):
        if (not isinstance(token, str) or not 24 <= len(token) <= 256 or not token.isascii()
                or not isinstance(version, str) or not 1 <= len(version) <= 128):
            raise ValueError('유효한 중앙 정책 토큰과 버전이 필요합니다.')
        self.governor = governor
        self.data_dir, self.token, self.version = Path(data_dir), token, version
        self.folder = None
        self._stop = threading.Event()
        self._thread = None
        self._metadata = None

    def start(self):
        if self._thread is not None:
            if self._thread.is_alive():
                return self
            raise RuntimeError('종료한 파일 IPC 서버를 다시 사용할 수 없습니다.')
        self.folder = mailbox(self.data_dir, create=True)
        try:
            previous = read_metadata(self.folder)
        except FileNotFoundError:
            previous = None
        if previous is not None and server_alive(previous):
            raise PermissionError('이 기록 폴더의 파일 IPC 서버가 이미 실행 중입니다.')
        self._metadata = {'app_id': APP_ID, 'protocol': PROTOCOL, 'version': self.version,
                          'server_pid': os.getpid(), 'server_created': psutil.Process().create_time(),
                          'token': self.token}
        _atomic_json(self.folder / 'server.json', self._metadata, METADATA_LIMIT, replace=previous is not None)
        self._thread = threading.Thread(target=self._run, name='resource-file-ipc', daemon=True)
        self._thread.start()
        return self

    def _owned_metadata(self):
        try:
            current = read_metadata(self.folder)
            return (current['token'] == self.token
                    and current['server_pid'] == self._metadata['server_pid']
                    and current['server_created'] == self._metadata['server_created'])
        except (OSError, ValueError, TypeError):
            return False

    def _remove_metadata(self):
        if self.folder is not None and self._metadata is not None and self._owned_metadata():
            _remove_regular(self.folder / 'server.json')

    def _handle(self, path, ident):
        try:
            request = _read_json(path, REQUEST_LIMIT)
            if (set(request) != {'protocol', 'id', 'nonce', 'operation', 'payload', 'signature'}
                    or request.get('protocol') != PROTOCOL or request.get('id') != ident
                    or not isinstance(request.get('nonce'), str) or not _NONCE.fullmatch(request['nonce'])
                    or not authenticated(self.token, request)):
                return
            response = {'protocol': PROTOCOL, 'id': ident, 'nonce': request['nonce']}
            try:
                operation = request['operation']
                if not isinstance(operation, str) or operation not in OPERATIONS:
                    raise ValueError('지원되는 중앙 정책 작업이 아닙니다.')
                response.update(ok=True, result=getattr(self.governor, operation)(request['payload']))
            except (ValueError, TypeError, KeyError):
                response.update(ok=False, error='ValueError', message='중앙 정책 요청이 유효하지 않습니다.')
            except PermissionError:
                response.update(ok=False, error='PermissionError', message='작업 임대 또는 프로세스 소유권을 확인할 수 없습니다.')
            except Exception:
                response.update(ok=False, error='RuntimeError', message='중앙 정책 처리에 실패하여 작업을 보류합니다.')
            _atomic_json(self.folder / (ident + '.response.json'), envelope(self.token, response), RESPONSE_LIMIT)
        except (OSError, ValueError, TypeError, OverflowError):
            # Never execute malformed input or follow a link. Only correctly
            # named regular incoming mail is consumed below.
            pass
        finally:
            try:
                _remove_regular(path)
            except OSError:
                pass

    def _run(self):
        try:
            while not self._stop.is_set():
                if not self._owned_metadata():
                    break
                try:
                    processed = 0
                    with os.scandir(self.folder) as entries:
                        for entry in entries:
                            if self._stop.is_set():
                                break
                            match = _REQUEST_NAME.fullmatch(entry.name)
                            if match is None:
                                continue
                            self._handle(self.folder / entry.name, match[1])
                            processed += 1
                            if processed >= 32:
                                break
                except OSError:
                    break
                self._stop.wait(.1)
        finally:
            self._remove_metadata()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            # The current request writes its response before the thread exits.
            # If storage stalls, metadata remains until that request finishes.
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                return
        self._remove_metadata()
