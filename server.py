"""자원 분석기: opt-in local collection and a browser dashboard."""
from __future__ import annotations
import argparse
import collections
import csv
from datetime import datetime
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import sys
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from collector import SAMPLE_INTERVAL_S
from sampling_worker import IsolatedCollector as Collector
from history import History, mean, gpu_value
from app_paths import APP_ID, VERSION, bundle_dir, state_dir
from governor import Governor
from resource_ipc import LocalIPC
from warning_history import WarningHistory
from runtime_state import write_intent
from temperature_metrics import TemperatureSampler

ROOT = bundle_dir()
STATIC = ROOT / 'static'
LOG = logging.getLogger('resource-analyzer.runtime')
SUMMARY_INTERVAL_S = 15


def history_frame(sample):
    """History aggregation never needs process lists, task logs or sensor payloads."""
    fields = ('id', 'name', 'cpu_pct', 'ram_gb', 'gpu_pct', 'io_read_mbps', 'io_write_mbps')
    system = sample['system']
    return {'ts': sample['ts'], 'sample_duration_s': sample['sample_duration_s'],
            'system': {key: system.get(key) for key in
                ('cpu_pct', 'ram_pct', 'disk_read_mbps', 'disk_write_mbps', 'gpus')},
            'groups': [{key: group.get(key) for key in fields} for group in sample['groups']]}


def next_sample_deadline(deadline, completed_at):
    # Work time belongs inside the one-second period, rather than being added
    # to it. A slow sample skips catch-up bursts; there is still one collector.
    return max(deadline + SAMPLE_INTERVAL_S, completed_at)


def stable_workload(before, after):
    if not before or not after or before.get('orphaned') or after.get('orphaned'):
        return None
    keys = ('stage', 'device', 'owner_pid', 'owner_created', 'request_id')
    if any(before.get(key) is None or before.get(key) != after.get(key) for key in keys):
        return None
    return {key: after[key] for key in ('stage', 'owner_pid', 'owner_created')}


def voice_status(data):
    try:
        config = json.loads((data / 'integrations.json').read_text(encoding='utf-8'))
        folder = Path(config['voice_status_dir'])
        if not folder.is_absolute():
            return {'available': False}
        workflow = json.loads((folder / 'workflow.json').read_text(encoding='utf-8-sig'))
        progress = json.loads((folder / 'progress.json').read_text(encoding='utf-8-sig'))
        counts = workflow.get('counts', {})
        # A stale status file is never presented as proof that production is alive.
        import psutil
        pid = workflow.get('pid')
        alive = bool(pid and psutil.pid_exists(pid))
        if alive:
            command = psutil.Process(pid).cmdline()
            alive = any(Path(arg).name == 'run_all.py' for arg in command)
        return {'available': True, 'status': workflow.get('status') if alive else '상태 파일만 존재 · 실행 미확인',
                'stage': progress.get('stage', ''), 'finished': counts.get('finished', 0),
                'total': workflow.get('total', 0), 'review': counts.get('review', 0), 'alive': alive,
                'single_gpu_fixed': bool(config.get('voice_single_gpu_fixed'))}
    except Exception:
        return {'available': False}


def make_insights(recent, voice):
    if not recent:
        return []
    last = recent[-1]['system']
    elapsed = recent[-1]['ts'] - recent[0]['ts']
    result = []
    if elapsed < 120:
        result.append({'level': 'info', 'title': '기록을 모으고 있습니다',
                       'detail': '순간 사용률은 구매 판단 근거가 부족합니다. 실제 작업을 계속하면서 프로그램별 추이와 반복되는 높은 사용률을 확인하세요.'})
    window = [x for x in recent if x['ts'] >= recent[-1]['ts'] - 300]
    cpu_busy = mean([float(x['system']['cpu_pct'] >= 85) for x in window])
    gpu_values = [gpu_value(x, 'util_pct') for x in window]
    gpu_busy = mean([float(v >= 90) for v in gpu_values if v is not None])
    ram_busy = mean([float(x['system']['ram_pct'] >= 90) for x in window])
    vram = gpu_value(recent[-1], 'vram_pct')
    if elapsed >= 120 and ram_busy >= .5:
        result.append({'level': 'warn', 'title': 'RAM 여유가 지속해서 적습니다',
                       'detail': f'최근 관측 구간의 {ram_busy*100:.0f}%에서 RAM 사용률이 90% 이상입니다. 현재 사용 가능 {last["ram_available_gb"]:.1f}GiB. 실제 느려짐과 함께 반복되면 RAM 증설 검토 근거가 됩니다.'})
    else:
        result.append({'level': 'info', 'title': f'RAM 사용 가능 {last["ram_available_gb"]:.1f}GiB',
                       'detail': 'RAM은 작업 공간입니다. 점유량만으로 속도 저하를 확정할 수 없으며, 필요한 작업을 함께 실행했을 때의 기록을 비교하세요.'})
    if vram is not None and vram >= 90:
        result.append({'level': 'watch', 'title': 'GPU 전용 메모리가 거의 찼습니다',
                       'detail': f'현재 전용 메모리 {vram:.1f}%. 모델 메모리 오류나 작업 제한이 함께 발생하면 더 큰 VRAM을 검토할 근거가 됩니다. GPU 연산 사용률과는 별개입니다.'})
    if elapsed >= 120 and gpu_busy is not None and gpu_busy >= .5:
        result.append({'level': 'watch', 'title': 'GPU 연산을 집중적으로 사용하는 작업입니다',
                       'detail': f'최근 관측 구간의 {gpu_busy*100:.0f}%에서 GPU 연산 사용률이 90% 이상입니다. 작업 시간 단축 가능성을 살펴볼 구간이며, 이 수치만으로 교체 필요성을 확정하지 않습니다.'})
    if elapsed >= 120 and cpu_busy >= .5:
        result.append({'level': 'watch', 'title': 'CPU 전체 부하가 지속됩니다',
                       'detail': f'최근 구간의 {cpu_busy*100:.0f}%에서 CPU가 85% 이상입니다. 어떤 프로그램이 차지하는지 아래 기록으로 확인하세요.'})
    elif last['cpu_pct'] < 40:
        result.append({'level': 'info', 'title': 'CPU 전체에는 여유가 있습니다',
                       'detail': f'현재 전체 {last["cpu_pct"]:.1f}%, 가장 바쁜 논리 코어 {last["cpu_peak_core_pct"]:.0f}%. CPU 사용률을 높이는 것만으로 GPU 작업이 빨라지지는 않습니다.'})
    if voice.get('alive') and voice.get('single_gpu_fixed'):
        result.append({'level': 'info', 'title': '마린 음성 엔진은 GPU 한 개를 사용합니다',
                       'detail': '현재 확인한 제작 구성은 GPU 0을 지정합니다. GPU를 하나 더 장착해도 이 작업이 자동 분산되지는 않으며, 작업 분리와 소프트웨어 변경·검증이 필요합니다.'})
    for disk in last.get('disks', []):
        if disk['used_pct'] >= 90:
            result.append({'level': 'watch', 'title': f'{disk["drive"]} 저장 공간이 적습니다',
                           'detail': f'남은 공간 {disk["free_gb"]:.1f}GiB. 저장 용량 부족과 디스크 처리 속도 부족은 서로 다른 문제입니다.'})
    result.append({'level': 'info', 'title': '디스크는 용량과 처리량을 따로 봅니다',
                   'detail': '프로그램 표의 I/O는 파일·장치 논리 I/O입니다. 상단 물리 디스크 처리량과 합계가 같지 않습니다. 현재 버전은 응답 지연을 측정하지 않아 SSD 속도 병목은 확정하지 않습니다.'})
    return result


class Monitor:
    def __init__(self, data, supervision_session=None):
        data.mkdir(parents=True, exist_ok=True)
        self.data = data
        self.history = History(data / 'usage.sqlite3')
        self.warning_history = WarningHistory(data / 'warning-events.sqlite3')
        self.warning_error = None
        self.storage_error = None
        self.recovery_attempts = 0
        self.sampling_worker_pid = None
        self.dropped_history_samples = 0
        self.intent_session = supervision_session or secrets.token_hex(16)
        self.intent_path = data / 'collection-intent.json'
        self.control_lock = threading.Lock()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.latest = None
        self.error = None
        self.started_at = time.time()
        self.thread = None
        self.collecting = False
        self.collection_requested = False
        self.recording_stop = threading.Event()
        self.csrf_token = secrets.token_urlsafe(32)
        self.governor = Governor(data)

    def start(self):
        with self.control_lock:
            with self.lock:
                if self.stop.is_set():
                    raise RuntimeError('Service is shutting down')
                if self.thread and self.thread.is_alive():
                    return
            write_intent(self.intent_path, self.intent_session, True)
            with self.lock:
                self.collection_requested = True
                self.recording_stop = threading.Event()
                self.collecting = True
                self.error = None
                self.thread = threading.Thread(target=self.run, name='resource-collector', daemon=True)
                self.thread.start()

    def pause(self, persist=True):
        with self.control_lock:
            if persist:
                write_intent(self.intent_path, self.intent_session, False)
                self.collection_requested = False
            self.governor.invalidate('자원 측정이 중지되었습니다')
            self.recording_stop.set()
            thread = self.thread
            if thread:
                thread.join(timeout=15)
            with self.lock:
                self.collecting = bool(thread and thread.is_alive())

    def close(self):
        self.stop.set()
        self.pause(persist=False)
        self.governor.close()

    def health(self):
        with self.lock:
            return {'app_id': APP_ID, 'version': VERSION, 'collecting': self.collecting,
                    'collection_requested': self.collection_requested,
                    'csrf_token': self.csrf_token, 'error': self.error,
                    'recovery_attempts': self.recovery_attempts, 'sampling_worker_pid': self.sampling_worker_pid,
                    'storage_error': self.storage_error, 'dropped_history_samples': self.dropped_history_samples,
                    'sample_age_s': max(0, time.time() - self.latest['ts']) if self.latest else None}

    def pause_warning_history(self, reason):
        try:
            self.warning_history.pause(reason)
        except Exception as error:
            self.warning_error = type(error).__name__ + ': ' + str(error)[:160]

    def warning_events(self, limit=50, before=None):
        result = self.warning_history.read(limit, before)
        with self.lock:
            result.update(collecting=self.collecting, stale=not self.latest or time.time() - self.latest['ts'] > 4,
                          recording_error=self.warning_error)
        return result

    def run(self):
        collector = None
        temperatures = None
        pending = collections.deque(maxlen=60)
        recent = collections.deque(maxlen=900)
        last_record = time.monotonic()
        last_sample = time.monotonic()
        cached_stats = {'started_at': None, 'samples': 0, 'duration_minutes': 0}
        failures = 0
        try:
            temperatures = TemperatureSampler()
            try:
                cached_stats = self.history.stats()
            except Exception as error:
                self.storage_error = type(error).__name__ + ': ' + str(error)[:160]
            deadline = last_sample + SAMPLE_INTERVAL_S
            while not self.recording_stop.wait(max(0, deadline - time.monotonic())):
                if self.stop.is_set():
                    break
                begin = time.monotonic()
                try:
                    if collector is None:
                        collector = Collector(cancel_event=self.recording_stop, temperature_sampler=temperatures)
                        self.sampling_worker_pid = collector.pid
                        if self.recording_stop.wait(SAMPLE_INTERVAL_S):
                            break
                        last_sample = time.monotonic()
                        begin = last_sample
                        LOG.info('Telemetry worker ready pid=%s', collector.pid)
                    workload_before = self.governor.status().get('active')
                    sample = collector.sample()
                    if workload_before:
                        workload = stable_workload(workload_before, self.governor.status().get('active'))
                        if workload:
                            sample['workload'] = workload
                    sample['sample_duration_s'] = begin - last_sample
                    sample['collection_duration_s'] = time.monotonic() - begin
                    last_sample = begin
                    sample['voice'] = voice_status(self.data)
                    recent.append({'ts': sample['ts'], 'system': sample['system']})
                    sample['insights'] = make_insights(recent, sample['voice'])
                    try:
                        if sample['collection_duration_s'] > 4:
                            self.warning_history.pause('measurement_gap')
                        else:
                            self.warning_history.record(sample)
                        self.warning_error = None
                    except Exception as error:
                        self.warning_error = type(error).__name__ + ': ' + str(error)[:160]
                        sample['errors'].append('주의·높음 기록 저장 실패: ' + self.warning_error)
                    if len(pending) == pending.maxlen:
                        self.dropped_history_samples += 1
                    pending.append(history_frame(sample))
                    if time.monotonic() - last_record >= SUMMARY_INTERVAL_S:
                        try:
                            self.history.record(list(pending))
                            pending.clear()
                            cached_stats = self.history.stats()
                            self.storage_error = None
                        except Exception as error:
                            self.storage_error = type(error).__name__ + ': ' + str(error)[:160]
                            LOG.warning('Summary storage temporarily unavailable: %s', self.storage_error)
                        last_record = time.monotonic()
                    sample['recording'] = {**cached_stats, 'retention_days': 7,
                                           'write_interval_s': 15, 'session_started_at': self.started_at}
                    self.governor.update(sample, observed_at=begin)
                    with self.lock:
                        sample['published_at'] = time.time()
                        self.latest = sample
                        self.error = None
                    if failures:
                        LOG.info('Telemetry collection recovered after %s consecutive failures', failures)
                    failures = 0
                except Exception as error:
                    if self.recording_stop.is_set() or self.stop.is_set():
                        break
                    self.pause_warning_history('measurement_gap')
                    self.governor.invalidate('자원 수집 오류로 새 작업을 기다립니다')
                    with self.lock:
                        self.error = '측정 엔진 자동 복구 중: ' + type(error).__name__ + ': ' + str(error)[:160]
                        self.recovery_attempts += 1
                    LOG.warning('Telemetry failure; restarting only the sampler', exc_info=True)
                    if collector is not None:
                        collector.close()
                        collector = None
                    self.sampling_worker_pid = None
                    failures += 1
                    if self.recording_stop.wait(min(30, 2 ** min(failures - 1, 5))):
                        break
                finally:
                    deadline = next_sample_deadline(deadline, time.monotonic())
        except Exception as error:
            self.governor.invalidate('자원 수집을 시작하지 못했습니다')
            with self.lock:
                self.error = type(error).__name__ + ': ' + str(error)[:160]
        finally:
            self.pause_warning_history('collection_stopped')
            self.governor.invalidate('자원 수집이 종료되었습니다')
            try:
                if pending:
                    try:
                        self.history.record(list(pending))
                    except Exception:
                        LOG.warning('Could not flush remaining summaries', exc_info=True)
                if collector:
                    collector.close()
            finally:
                if temperatures is not None:
                    temperatures.close()
                with self.lock:
                    self.collecting = False
                    self.sampling_worker_pid = None
                LOG.info('Collection thread stopped')

    def snapshot(self):
        with self.lock:
            if self.latest is None:
                return {'status': 'starting' if self.collecting else 'idle', 'error': self.error}, 503
            result = dict(self.latest)
            result['errors'] = list(result['errors'])
            if self.error:
                result['errors'].append('수집 일시 중단: ' + self.error)
            if self.storage_error:
                result['errors'].append('요약 기록 저장 재시도 중: ' + self.storage_error)
            if self.dropped_history_samples:
                result['errors'].append(f'저장 지연 중 요약 표본 {self.dropped_history_samples}개 보관 한도 초과')
            result['stale'] = time.time() - result['ts'] > 4
            result['collecting'] = self.collecting
            return result, 200


def csv_safe(value):
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def make_handler(monitor, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, content_type, status=200, disposition=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https://speed.cloudflare.com/__down https://speed.cloudflare.com/__up; frame-ancestors 'none'")
            if disposition:
                self.send_header('Content-Disposition', disposition)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Browser refresh/closed tabs are not service failures.
                pass

        def do_GET(self):
            if self.headers.get('Host', '') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                self.send(b'Invalid local host', 'text/plain', 403)
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                minutes = max(1, min(10080, int(query.get('minutes', ['60'])[0])))
            except (ValueError, TypeError):
                self.send(b'Invalid time range', 'text/plain', 400)
                return
            if parsed.path == '/api/health':
                self.send(json.dumps(monitor.health(), ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            elif parsed.path == '/api/governor':
                self.send(json.dumps(monitor.governor.status(), ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')
            elif parsed.path == '/api/snapshot':
                value, status = monitor.snapshot()
                self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8', status)
            elif parsed.path == '/api/history':
                self.send(json.dumps(monitor.history.read(minutes), ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')
            elif parsed.path == '/api/warning-events':
                try:
                    limit = int(query.get('limit', ['50'])[0])
                    before = int(query['before'][0]) if 'before' in query else None
                    if before is not None and not 1 <= before <= 9223372036854775807:
                        raise ValueError('Invalid cursor')
                    value = monitor.warning_events(limit, before)
                except (ValueError, TypeError):
                    self.send(b'Invalid warning history cursor', 'text/plain', 400)
                    return
                except Exception:
                    self.send(b'{"error":"Warning history is temporarily unavailable"}', 'application/json', 503)
                    return
                self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')
            elif parsed.path == '/api/export.csv':
                kind = 'groups' if query.get('kind', ['system'])[0] == 'groups' else 'system'
                output = io.StringIO()
                writer = csv.writer(output)
                header = None
                for row in monitor.history.export_rows(kind, minutes):
                    row = {'time_local': datetime.fromtimestamp(row['ts']).astimezone().isoformat(timespec='seconds'), **row}
                    if header is None:
                        header = list(row)
                        writer.writerow(header)
                    writer.writerow([csv_safe(row[k]) for k in header])
                self.send(output.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', disposition=f'attachment; filename="resource-analyzer-{kind}.csv"')
            else:
                name = {'/': 'welcome.html', '/index.html': 'welcome.html',
                        '/dashboard': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                        '/governor.js': 'governor.js',
                        '/speedtest.mjs': 'speedtest.mjs',
                        '/resource-report.mjs': 'resource-report.mjs',
                        '/device-cards.mjs': 'device-cards.mjs',
                        '/resource-rules.mjs': 'resource-rules.mjs',
                        '/warning-history.mjs': 'warning-history.mjs',
                        '/task-labels.mjs': 'task-labels.mjs',
                        '/vendor/cloudflare-speedtest-1.14.2.mjs': 'vendor/cloudflare-speedtest-1.14.2.mjs',
                        '/welcome.js': 'welcome.js', '/welcome.css': 'welcome.css'}.get(parsed.path)
                if not name or not (STATIC / name).is_file():
                    self.send(b'Not found', 'text/plain', 404)
                    return
                # Windows registry associations differ across PCs. ES modules
                # require a JavaScript MIME type when nosniff is enabled.
                mime = {'.html': 'text/html', '.css': 'text/css',
                        '.js': 'text/javascript', '.mjs': 'text/javascript'}[Path(name).suffix]
                content = (STATIC / name).read_bytes()
                if mime == 'text/html':
                    content = content.replace(b'{{APP_VERSION}}', VERSION.encode('ascii'))
                self.send(content, mime + '; charset=utf-8')

        def do_POST(self):
            allowed = (f'127.0.0.1:{port}', f'localhost:{port}')
            origins = tuple('http://' + host for host in allowed)
            if self.headers.get('Host', '') not in allowed or self.headers.get('Origin') not in (None, *origins):
                self.send(b'Invalid local origin', 'text/plain', 403)
                return
            supplied = self.headers.get('X-Resource-Analyzer-Token', '')
            if not secrets.compare_digest(supplied, monitor.csrf_token):
                self.send(b'Invalid local control token', 'text/plain', 403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 1024:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, json.JSONDecodeError):
                self.send(b'Invalid request', 'text/plain', 400)
                return
            path = urlparse(self.path).path
            if path.startswith('/api/governor/'):
                try:
                    if path == '/api/governor/configure':
                        if set(payload) != {'enabled'} or not isinstance(payload['enabled'], bool):
                            raise ValueError('enabled must be boolean')
                        value = monitor.governor.configure(payload['enabled'])
                        if payload['enabled']:
                            monitor.start()
                    elif path == '/api/governor/acquire':
                        value = monitor.governor.acquire(payload)
                    elif path == '/api/governor/checkpoint':
                        value = monitor.governor.checkpoint(payload)
                    elif path == '/api/governor/release':
                        value = monitor.governor.release(payload)
                    else:
                        self.send(b'Not found', 'text/plain', 404)
                        return
                except PermissionError:
                    self.send(b'Invalid worker ownership', 'text/plain', 403)
                    return
                except (ValueError, TypeError, KeyError):
                    self.send(b'Invalid governor request', 'text/plain', 400)
                    return
                self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')
                return
            if path == '/api/monitor/start':
                monitor.start()
            elif path == '/api/monitor/stop':
                monitor.pause()
            else:
                self.send(b'Not found', 'text/plain', 404)
                return
            self.send(json.dumps(monitor.health(), ensure_ascii=False).encode(), 'application/json; charset=utf-8')
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=7442)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--autostart', action='store_true')
    parser.add_argument('--supervision-session', help=argparse.SUPPRESS)
    args = parser.parse_args()
    data = state_dir(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    if args.stop:
        (data / 'STOP').write_text('Stop resource monitor only.', encoding='utf-8')
        if sys.stdout is not None:
            print('Resource monitor will stop and flush history within a few seconds.')
        return
    handler = RotatingFileHandler(data / 'runtime.log', maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    LOG.info('Service starting version=%s pid=%s', VERSION, os.getpid())
    monitor = Monitor(data, supervision_session=args.supervision_session)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(monitor, args.port))
    ipc = LocalIPC(monitor.governor, data, monitor.csrf_token, VERSION)
    try:
        ipc.start()
    except Exception:
        server.server_close()
        monitor.close()
        raise
    if not args.supervision_session:
        (data / 'STOP').unlink(missing_ok=True)
    (data / 'monitor.pid').write_text(str(os.getpid()), encoding='ascii')
    if args.autostart or monitor.governor.status()['enabled']:
        monitor.start()
    def shutdown_when_requested():
        while not monitor.stop.wait(.3):
            if (data / 'STOP').exists():
                monitor.stop.set()
                break
        server.shutdown()
    threading.Thread(target=shutdown_when_requested, daemon=True).start()
    if sys.stdout is not None:
        print(f'Resource Analyzer: http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        ipc.close()
        monitor.close()
        server.server_close()
        (data / 'monitor.pid').unlink(missing_ok=True)
        LOG.info('Service stopped')
        LOG.removeHandler(handler)
        handler.close()


if __name__ == '__main__':
    main()
