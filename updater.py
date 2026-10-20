"""User-invoked package update, without changing Windows script policy."""
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.request import ProxyHandler, build_opener

from app_paths import VERSION


def apply_update(args, directory):
    import entrypoint

    if not getattr(sys, 'frozen', False):
        raise RuntimeError('Run the packaged ResourceAnalyzer.exe to apply an update.')
    executable = Path(sys.executable)
    info = json.loads((executable.parent / 'BUILD-INFO.txt').read_text(encoding='utf-8-sig'))
    with executable.open('rb') as binary:
        digest = hashlib.file_digest(binary, 'sha256').hexdigest()
    if info.get('app_version') != VERSION or digest != info.get('executable_sha256'):
        raise RuntimeError('Package verification failed. Nothing was changed.')
    current = entrypoint.health(args.port)
    if not current and entrypoint.port_busy(args.port):
        raise RuntimeError('The port belongs to an unidentified application. Nothing was changed.')
    if current:
        with build_opener(ProxyHandler({})).open(
                f'http://127.0.0.1:{args.port}/api/governor', timeout=3) as response:
            governor = json.load(response)
        if governor.get('active') is not None:
            raise RuntimeError('An allocated worker is active. Wait for it to finish and try again.')
        if current.get('version') == VERSION and any(
                item.get('stage') == 'qwen-vision' for item in governor.get('adapters', [])):
            return entrypoint.launch(args, directory)
        args.autostart = bool(current.get('collecting'))
        args.stop = True
        try:
            result = entrypoint.serve(args)
        finally:
            args.stop = False
        if result:
            raise RuntimeError('Normal shutdown failed. No process was killed.')
        deadline = time.monotonic() + 30
        while entrypoint.port_busy(args.port):
            if time.monotonic() >= deadline:
                raise RuntimeError('Normal shutdown is still pending. No process was killed.')
            time.sleep(.25)
    result = entrypoint.launch(args, directory)
    updated = entrypoint.health(args.port)
    if not updated or updated.get('version') != VERSION:
        raise RuntimeError('The response does not match the selected package. Check the local log.')
    return result
