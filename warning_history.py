"""Durable warning episodes, independent of browser tabs and seven-day summaries."""
from contextlib import contextmanager
import json
import math
import sqlite3
import threading

from app_paths import bundle_dir

# A JSON object with an ES-module prefix gives Python and the browser one set of rules.
_rules = (bundle_dir() / 'static' / 'resource-rules.mjs').read_text(encoding='utf-8').strip()
if not _rules.startswith('export default ') or not _rules.endswith(';'):
    raise ValueError('Invalid shared resource rules')
RULES = json.loads(_rules[len('export default '):-1])
NAMES = {'cpu': 'CPU', 'gpu': 'GPU', 'ram': '메모리', 'disk': '디스크'}
STAGES = {'qwen-vision': 'Qwen 화면 분석', 'qwen-summary': 'Qwen 결과 요약',
          'whisper-asr': '음성 인식', 'cosyvoice-synthesis': '음성 합성', 'audio-preprocess': '음성 전처리'}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def number(value, digits=1):
    return f'{value:,.{digits}f}' if finite(value) else '—'


def observed_activity(snapshot, group, resource):
    if not group:
        return {'program': '미확인', 'task': '미확인'}
    processes = group.get('processes') or []
    field = {'cpu': 'cpu_pct', 'gpu': 'gpu_pct', 'ram': 'ram_gb'}.get(resource)
    def weight(process):
        if resource == 'disk':
            return sum(process.get(k) or 0 for k in ('io_read_mbps', 'io_write_mbps') if finite(process.get(k)))
        return process.get(field) if finite(process.get(field)) else -1
    process = max(processes, key=weight, default={})
    workload = snapshot.get('workload') or {}
    # A coincident task in another app (or a reused PID) is not attribution.
    owner = next((p for p in processes if p.get('pid') == workload.get('owner_pid')
                  and finite(p.get('created_at')) and finite(workload.get('owner_created'))
                  and abs(p['created_at'] - workload['owner_created']) < .01), None)
    if owner and workload.get('stage') in STAGES:
        task = STAGES[workload['stage']]
    elif process.get('script'):
        task = process['script']
    elif resource == 'gpu':
        engine = str(group.get('gpu_engine') or '').lower()
        task = next((label for key, label in [('videoencode', '영상 인코딩'), ('videodecode', '영상 디코딩'),
                    ('compute', 'GPU 연산'), ('cuda', 'GPU 연산'), ('3d', '3D 렌더링'), ('copy', 'GPU 데이터 전송')]
                     if key in engine.replace('_', '').replace(' ', '')), '세부 작업 미확인')
    else:
        task = '세부 작업 미확인'
    if task == '세부 작업 미확인' and process.get('name'):
        task = process['name'] + ' · 세부 작업 미확인'
    return {'program': group.get('name') or '이름 미확인', 'task': task}


def warning_reports(snapshot):
    """Return measured values (including healthy ones) and warning evidence."""
    system = snapshot.get('system') or {}
    groups = snapshot.get('groups') or []
    gpus = system.get('gpus') or []
    gpu = max(gpus, key=lambda g: g.get('util_pct') if finite(g.get('util_pct')) else -1, default={})
    measured, reports, activities = {}, [], {}

    def add(key, title, value, limits, unit, evidence, note, cause='', activity=None):
        measured[key] = value if finite(value) else None
        if not finite(value) or value < limits[0]:
            return
        level = 'high' if value >= limits[1] else 'watch'
        threshold = limits[1] if level == 'high' else limits[0]
        reports.append({'id': key, 'title': title, 'level': level, 'numeric_value': value,
                        'value': number(value) + unit,
                        'reason': f'현재 {number(value)}{unit}로 {"높음" if level == "high" else "주의"} 표시 기준 {threshold}{unit} 이상입니다.',
                        'evidence': evidence, 'note': note, 'cause': str(cause),
                        **(activity or {'program': '미확인', 'task': '미확인'})})

    for key, value in {'cpu': system.get('cpu_pct'), 'gpu': gpu.get('util_pct'),
                       'ram': system.get('ram_pct'), 'disk': system.get('disk_busy_pct')}.items():
        field = {'cpu': 'cpu_pct', 'gpu': 'gpu_pct', 'ram': 'ram_gb', 'disk': 'io_total'}[key]
        candidates = []
        for group in groups:
            item = dict(group)
            if key == 'disk':
                read, write = item.get('io_read_mbps'), item.get('io_write_mbps')
                item[field] = read + write if finite(read) and finite(write) else None
            if finite(item.get(field)) and item[field] > 0:
                candidates.append(item)
        if key == 'gpu' and snapshot.get('coverage', {}).get('process_gpu_available') is False:
            candidates = []
        top = sorted(candidates, key=lambda g: g[field], reverse=True)[:3]
        unit = ' GiB' if key == 'ram' else ' MiB/s' if key == 'disk' else '%'
        evidence = '해당 자원의 프로그램별 측정값이 없어 사용 프로그램을 특정할 수 없습니다.'
        if top:
            evidence = ('같은 시점 논리 I/O 상위: ' if key == 'disk' else '같은 시점 사용량 상위: ') + ' · '.join(
                f'{g.get("name") or "이름 미확인 프로그램"} {number(g[field])}{unit}' for g in top)
        note = {
            'cpu': '전체 CPU 용량 기준이며 높은 사용률만으로 오류나 과열을 뜻하지는 않습니다.',
            'gpu': f'측정 장치: {gpu.get("name") or "이름 미확인 GPU"}. 프로그램 합계는 장치 사용률과 표본 시점·집계 방식이 다릅니다.',
            'ram': f'사용 {number(system.get("ram_used_gb"))} / {number(system.get("ram_total_gb"))} GiB · 여유 {number(system.get("ram_available_gb"))} GiB. 공유 메모리는 중복될 수 있고 전체에는 OS·캐시도 포함됩니다.',
            'disk': f'물리 디스크 읽기 {number(system.get("disk_read_mbps"))} · 쓰기 {number(system.get("disk_write_mbps"))} MiB/s. 논리 I/O와 달라 특정 프로그램·드라이브가 원인이라고 확정할 수 없습니다.',
        }[key]
        cause = str(top[0].get('id') or top[0].get('name')) if top else ''
        activity = observed_activity(snapshot, top[0] if top else None, key)
        activities[key] = activity
        cause += '|' + activity['task']
        if key == 'gpu':
            cause += '|' + str(gpu.get('name', ''))
        add(key + '-usage', NAMES[key] + (' 활동률' if key == 'disk' else ' 사용률'), value,
            RULES['usage'][key], '%', evidence, note, cause, activity)

    temperatures = system.get('temperatures') or {}
    gpu_sensors = [s for s in (temperatures.get('gpu') or {}).get('sensors', []) if finite(s.get('temp_c'))]
    gpu_sensors += [{'name': g.get('name') or 'GPU', 'temp_c': g['temp_c']} for g in gpus if finite(g.get('temp_c'))]
    hottest = max(gpu_sensors, key=lambda s: s['temp_c'], default=temperatures.get('gpu') or {})
    for key in NAMES:
        sensor = hottest if key == 'gpu' else temperatures.get(key) or {}
        evidence = f'온도 센서: {sensor["name"]}' if sensor.get('name') else sensor.get('details') or '장치 온도 센서의 실측값입니다.'
        add(key + '-temperature', NAMES[key] + (' 최고 온도' if key == 'gpu' else ' 온도'), sensor.get('temp_c'),
            RULES['thermal'][key], '°C', evidence,
            '표시 참고 기준입니다. 온도만으로 냉각·팬·주변 온도 등 물리적인 상승 원인을 확정할 수 없습니다.',
            sensor.get('name', ''), {**activities[key], 'simultaneous': True})

    network = system.get('network') or {}
    disconnected = network.get('kind') == 'disconnected'
    measured['network-disconnected'] = 1 if disconnected else 0 if network.get('status') == 'ok' else None
    if disconnected:
        reports.append({'id': 'network-disconnected', 'title': '네트워크 연결', 'level': 'high', 'numeric_value': 1,
                        'value': '연결 없음', 'reason': '사용 가능한 기본 네트워크 경로가 확인되지 않아 높음으로 표시합니다.',
                        'evidence': network.get('name') or '기본 경로 측정 결과입니다.',
                        'note': '케이블·공유기·통신사 중 어느 구간의 문제인지는 이 측정만으로 확정할 수 없습니다.', 'cause': network.get('name', '')})
    for direction, field in [('다운로드', 'download_mbps'), ('업로드', 'upload_mbps')]:
        value, link = network.get(field), network.get('link_mbps')
        ratio = value / link * 100 if network.get('status') == 'ok' and finite(value) and finite(link) and link > 0 else None
        add('network-' + field, '네트워크 ' + direction + ' 사용률', ratio, RULES['usage']['network'], '%',
            f'{network.get("name") or "현재 어댑터"} · {direction} {number(value, 2)} Mbps / 연결 {number(link, 0)} Mbps',
            '연결 속도 대비 전송량이며 인터넷 상품 속도나 고장 판정이 아닙니다. 프로그램별 네트워크 원인은 수집하지 않습니다.', network.get('name', ''))
    return measured, reports


class WarningHistory:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS warning_events (
                event_id INTEGER PRIMARY KEY, metric TEXT NOT NULL, signature TEXT NOT NULL,
                started_at REAL NOT NULL, last_seen REAL NOT NULL, ended_at REAL,
                end_reason TEXT, sample_count INTEGER NOT NULL, peak REAL NOT NULL, peak_at REAL,
                report TEXT NOT NULL, latest_report TEXT NOT NULL);
              CREATE UNIQUE INDEX IF NOT EXISTS active_warning_metric ON warning_events(metric) WHERE ended_at IS NULL;
              CREATE INDEX IF NOT EXISTS warning_history_time ON warning_events(started_at);
            ''')
            if 'peak_at' not in {row['name'] for row in db.execute('PRAGMA table_info(warning_events)')}:
                db.execute('ALTER TABLE warning_events ADD COLUMN peak_at REAL')
            # A restart is an observation gap, never evidence of recovery.
            db.execute("UPDATE warning_events SET ended_at=last_seen,end_reason='restart' WHERE ended_at IS NULL")
            self.last_ts = db.execute('SELECT COALESCE(MAX(last_seen),0) FROM warning_events').fetchone()[0]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def pause(self, reason='measurement_gap'):
        with self.lock, self.connect() as db:
            db.execute('UPDATE warning_events SET ended_at=last_seen,end_reason=? WHERE ended_at IS NULL', (reason,))

    def record(self, snapshot):
        ts = snapshot.get('ts')
        if not finite(ts) or snapshot.get('stale'):
            self.pause()
            return
        measured, reports = warning_reports(snapshot)
        reports = {r['id']: r for r in reports}
        with self.lock:
            with self.connect() as db:
                if ts <= self.last_ts:
                    return
                active = {r['metric']: dict(r) for r in db.execute('SELECT * FROM warning_events WHERE ended_at IS NULL')}
                for key, old in list(active.items()):
                    report = reports.get(key)
                    signature = json.dumps([report['level'], report['cause']], ensure_ascii=False) if report else None
                    if signature != old['signature']:
                        reason = 'changed' if report else 'recovered' if measured.get(key) is not None else 'measurement_gap'
                        db.execute('UPDATE warning_events SET ended_at=?,end_reason=? WHERE event_id=?', (ts, reason, old['event_id']))
                        del active[key]
                for key, report in reports.items():
                    payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
                    old = active.get(key)
                    if old:
                        higher = report['numeric_value'] > old['peak']
                        db.execute('UPDATE warning_events SET last_seen=?,sample_count=sample_count+1,peak=?,peak_at=?,report=?,latest_report=? WHERE event_id=?',
                                   (ts, max(report['numeric_value'], old['peak']), ts if higher else old['peak_at'], payload if higher else old['report'], payload, old['event_id']))
                    else:
                        signature = json.dumps([report['level'], report['cause']], ensure_ascii=False)
                        db.execute('INSERT INTO warning_events(metric,signature,started_at,last_seen,sample_count,peak,peak_at,report,latest_report) VALUES(?,?,?,?,1,?,?,?,?)',
                                   (key, signature, ts, ts, report['numeric_value'], ts, payload, payload))
            self.last_ts = ts

    def read(self, limit=50, before=None):
        limit = max(1, min(100, int(limit)))
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM warning_events').fetchone()[0]
            active_count = db.execute('SELECT COUNT(*) FROM warning_events WHERE ended_at IS NULL').fetchone()[0]
            where, values = ('WHERE event_id < ?', [before]) if before is not None else ('', [])
            data = list(db.execute(f'SELECT * FROM warning_events {where} ORDER BY event_id DESC LIMIT ?', [*values, limit + 1]))
        more = len(data) > limit
        events = []
        for row in data[:limit]:
            event = {key: row[key] for key in ('event_id', 'started_at', 'last_seen', 'peak_at', 'ended_at', 'end_reason', 'sample_count')}
            event.update(json.loads(row['report']))
            event['last_value'] = json.loads(row['latest_report'])['value']
            events.append(event)
        return {'events': events, 'total': total, 'active_count': active_count,
                'next_before': events[-1]['event_id'] if more and events else None}
