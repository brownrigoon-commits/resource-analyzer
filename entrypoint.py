"""Double-click launcher and hidden loopback server entry point."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser

from app_paths import APP_ID, APP_NAME, VERSION, state_dir


def local_json(port, path, timeout=.8):
    """Read only this loopback service, without proxy or unrelated TLS setup."""
    connection = HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f'Unexpected local HTTP status {response.status}')
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError('Local control response exceeded the size limit')
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError('Local control response is not an object')
        return value
    finally:
        connection.close()


def health(port):
    """Never send local health requests through a configured internet proxy."""
    try:
        value = local_json(port, '/api/health')
        if value.get('app_id') != APP_ID:
            logging.warning('Local health probe returned an unexpected application identity on port %s', port)
            return None
        return value
    except (OSError, ValueError, HTTPException) as error:
        logging.info('Local health probe failed on port %s (%s): %s', port, type(error).__name__, error)
        return None


def port_busy(port):
    with socket.socket() as sock:
        sock.settimeout(.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


@contextmanager
def launch_lock(directory, port):
    """Serialize launchers, without retaining a PID that could be reused."""
    lock = (directory / f'launcher-{port}.lock').open('a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    deadline = time.monotonic() + 30
    while True:
        try:
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                lock.close()
                raise RuntimeError('다른 실행이 준비 중입니다. 잠시 후 다시 실행해 주세요.')
            time.sleep(.1)
    try:
        yield
    finally:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def server_args(args):
    result = ['--port', str(args.port)]
    if args.data_dir:
        result += ['--data-dir', str(state_dir(args.data_dir))]
    if args.autostart:
        result.append('--autostart')
    if args.stop:
        result.append('--stop')
    return result


def serve(args):
    # Lazy import keeps browser-only launches fast and package discovery explicit.
    import server
    sys.argv = [sys.argv[0], *server_args(args)]
    return server.main()


def launch(args, directory):
    url = f'http://127.0.0.1:{args.port}/'
    with launch_lock(directory, args.port):
        if not health(args.port):
            if port_busy(args.port):
                raise RuntimeError(
                    f'{args.port} 포트를 다른 프로그램이 사용 중입니다. '
                    '그 프로그램을 확인하거나 다른 --port 번호로 실행해 주세요.')
            if getattr(sys, 'frozen', False):
                command = [sys.executable, '--serve', *server_args(args)]
            else:
                command = [sys.executable, str(Path(__file__).resolve()),
                           '--serve', *server_args(args)]
            environment = dict(os.environ)
            if getattr(sys, 'frozen', False):
                # Required for an independent onefile child that outlives us.
                environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            flags = 0
            if os.name == 'nt':
                flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            with (directory / 'server.log').open('ab') as log:
                process = subprocess.Popen(command, cwd=str(directory), env=environment,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           creationflags=flags, close_fds=True)
            deadline = time.monotonic() + 30
            while not health(args.port):
                if process.poll() is not None:
                    raise RuntimeError('분석기를 시작하지 못했습니다. 로컬 server.log를 확인해 주세요.')
                if time.monotonic() >= deadline:
                    raise RuntimeError('분석기 시작을 기다리고 있습니다. 잠시 후 다시 실행해 주세요.')
                time.sleep(.15)
    if not args.no_browser:
        webbrowser.open(url, new=2)
    return 0


def main():
    parser = argparse.ArgumentParser(description=f'{APP_NAME} — 로컬 PC 사용량 분석')
    parser.add_argument('--version', action='version', version=f'{APP_NAME} {VERSION}')
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=7442)
    parser.add_argument('--data-dir', help='기록과 로그를 저장할 폴더')
    parser.add_argument('--autostart', action='store_true', help='수집도 즉시 시작')
    parser.add_argument('--stop', action='store_true', help='현재 분석기 서비스 종료')
    parser.add_argument('--apply-update', action='store_true', help='사용자가 선택한 배포본을 검증하고 정상 갱신')
    parser.add_argument('--no-browser', action='store_true', help='브라우저를 열지 않음')
    parser.add_argument('--install-temperature-support', action='store_true', help='공식 온도 센서 드라이버 설치')
    args = parser.parse_args()
    if args.install_temperature_support:
        from temperature_metrics import launch_elevated
        from app_paths import bundle_dir
        installer = bundle_dir() / 'vendor' / 'sensors' / 'PawnIO_setup.exe'
        if not installer.is_file():
            raise RuntimeError('온도 센서 설치 파일이 포함되지 않은 빌드입니다.')
        launch_elevated(installer, ['-install'])
        return 0
    if not 1 <= args.port <= 65535:
        parser.error('포트는 1부터 65535 사이여야 합니다.')
    directory = state_dir(args.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=directory / 'launcher.log', encoding='utf-8',
                        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.apply_update:
            from updater import apply_update
            return apply_update(args, directory)
        if args.serve or args.stop:
            return serve(args)
        return launch(args, directory)
    except Exception as error:
        logging.exception('Application startup failed')
        message = f'{APP_NAME}\n\n{error}'
        if sys.stderr:
            print(message, file=sys.stderr)
        if os.name == 'nt' and not args.no_browser and not args.serve:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
