"""Recover this app's exited server. Never kill it or another application's work."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import uuid

from runtime_state import read_intent

LOG = logging.getLogger('resource-analyzer.supervisor')


def wait_for_stop(path, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
    return path.exists()


def supervise(args, directory):
    import entrypoint
    directory = Path(directory)
    handler = RotatingFileHandler(directory / 'supervisor.log', maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    try:
        with entrypoint.launch_lock(directory, f'supervisor-{args.port}', timeout=8):
            return watch_server(args, directory)
    finally:
        LOG.removeHandler(handler)
        handler.close()


def watch_server(args, directory):
    import entrypoint
    stop_file = directory / 'STOP'
    stop_file.unlink(missing_ok=True)
    args.supervision_session = uuid.uuid4().hex
    failures = 0
    while True:
        # A server started by someone else must never be replaced.
        if entrypoint.port_busy(args.port):
            LOG.info('Port already occupied; supervisor exits without touching it')
            return 0
        command = entrypoint.background_command('--serve', args)
        started = time.monotonic()
        try:
            with (directory / 'server.log').open('ab') as log:
                child = entrypoint.background_process(command, directory, log)
                LOG.info('Started server pid=%s', child.pid)
                code = child.wait()
        except OSError:
            LOG.warning('Could not start server; retrying', exc_info=True)
            code = None
        if stop_file.exists():
            LOG.info('Explicit service shutdown; supervisor stopped')
            return 0
        args.autostart = read_intent(directory / 'collection-intent.json', args.supervision_session, False)
        if time.monotonic() - started >= 300:
            failures = 0
        delay = min(60, 2 ** min(failures, 6))
        failures += 1
        LOG.warning('Server exited unexpectedly code=%s; retry in %ss, collecting=%s', code, delay, args.autostart)
        if wait_for_stop(stop_file, delay):
            return 0
