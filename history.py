"""Local SQLite history, 15-second summaries, seven-day retention."""
import collections
from contextlib import contextmanager
import sqlite3
import statistics
import time


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def peak(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def gpu_value(snapshot, field):
    return peak([g.get(field) for g in snapshot['system'].get('gpus', [])])


class History:
    def __init__(self, path):
        self.path = str(path)
        self.last_cleanup = 0
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS system_samples (
                ts REAL PRIMARY KEY, cpu_pct REAL, cpu_peak REAL, ram_pct REAL,
                ram_peak REAL, gpu_pct REAL, gpu_peak REAL, vram_pct REAL,
                disk_read_mbps REAL, disk_write_mbps REAL,
                cpu_busy_fraction REAL, gpu_busy_fraction REAL,
                ram_pressure_fraction REAL, duration_s REAL);
              CREATE TABLE IF NOT EXISTS group_samples (
                ts REAL, id TEXT, name TEXT, cpu_avg REAL, cpu_peak REAL,
                ram_peak_gb REAL, gpu_avg REAL, gpu_peak REAL, io_total_gb REAL,
                PRIMARY KEY(ts,id));
              CREATE INDEX IF NOT EXISTS group_by_identity ON group_samples(id,ts);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, snapshots):
        if not snapshots:
            return
        ts = snapshots[-1]['ts']
        systems = [s['system'] for s in snapshots]
        duration = sum(min(30, max(.1, s.get('sample_duration_s', 1))) for s in snapshots)
        cpu = [s['cpu_pct'] for s in systems]
        ram = [s['ram_pct'] for s in systems]
        gpu = [gpu_value(s, 'util_pct') for s in snapshots]
        valid_gpu = [v for v in gpu if v is not None]
        sysrow = (ts, mean(cpu), peak(cpu), mean(ram), peak(ram), mean(gpu), peak(gpu),
                  mean([gpu_value(s, 'vram_pct') for s in snapshots]),
                  mean([s['disk_read_mbps'] for s in systems]), mean([s['disk_write_mbps'] for s in systems]),
                  mean([float(v >= 85) for v in cpu]), mean([float(v >= 90) for v in valid_gpu]),
                  mean([float(v >= 90) for v in ram]), duration)
        grouped = collections.defaultdict(list)
        for s in snapshots:
            for g in s['groups']:
                grouped[g['id']].append(g)
        rows = []
        for key, values in grouped.items():
            rows.append((ts, key, values[-1]['name'], sum(g['cpu_pct'] for g in values) / len(snapshots),
                         peak([g['cpu_pct'] for g in values]), peak([g['ram_gb'] for g in values]),
                         mean([g['gpu_pct'] for g in values]), peak([g['gpu_pct'] for g in values]),
                         sum(g['io_read_mbps'] + g['io_write_mbps'] for g in values) / len(snapshots) * duration / 1024))
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO system_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', sysrow)
            db.executemany('INSERT OR REPLACE INTO group_samples VALUES(?,?,?,?,?,?,?,?,?)', rows)
            if ts - self.last_cleanup >= 3600:
                cutoff = ts - 7 * 86400
                db.execute('DELETE FROM system_samples WHERE ts < ?', (cutoff,))
                db.execute('DELETE FROM group_samples WHERE ts < ?', (cutoff,))
                self.last_cleanup = ts

    def stats(self):
        with self.connect() as db:
            return dict(db.execute('SELECT MIN(ts) AS started_at, COUNT(*) AS samples, COALESCE(SUM(duration_s),0)/60 AS duration_minutes FROM system_samples').fetchone())

    def read(self, minutes=60):
        minutes = max(1, min(10080, int(minutes)))
        cutoff = time.time() - minutes * 60
        bucket = max(15, minutes * 60 / 240)
        with self.connect() as db:
            system = [dict(r) for r in db.execute('''
              SELECT MAX(ts) AS ts, AVG(cpu_pct) AS cpu_pct, MAX(cpu_peak) AS cpu_peak,
                     AVG(ram_pct) AS ram_pct, MAX(ram_peak) AS ram_peak,
                     AVG(gpu_pct) AS gpu_pct, MAX(gpu_peak) AS gpu_peak,
                     AVG(vram_pct) AS vram_pct,
                     AVG(disk_read_mbps) AS disk_read_mbps, AVG(disk_write_mbps) AS disk_write_mbps
              FROM system_samples WHERE ts>=? GROUP BY CAST(ts/? AS INTEGER) ORDER BY ts
            ''', (cutoff, bucket))]
            groups = [dict(r) for r in db.execute('''
              SELECT name, AVG(cpu_avg) AS cpu_avg, MAX(cpu_peak) AS cpu_peak,
                     MAX(ram_peak_gb) AS ram_peak_gb, AVG(gpu_avg) AS gpu_avg,
                     MAX(gpu_peak) AS gpu_peak, SUM(io_total_gb) AS io_total_gb,
                     COUNT(*) AS observed_samples
              FROM group_samples WHERE ts>=? GROUP BY id ORDER BY cpu_avg DESC
            ''', (cutoff,))]
            coverage = dict(db.execute('''SELECT MIN(ts) AS started_at, COUNT(*) AS samples,
              COALESCE(SUM(duration_s),0)/60 AS duration_minutes,
              AVG(cpu_busy_fraction)*100 AS cpu_busy_share,
              AVG(gpu_busy_fraction)*100 AS gpu_busy_share,
              AVG(ram_pressure_fraction)*100 AS ram_pressure_share
              FROM system_samples WHERE ts>=?''', (cutoff,)).fetchone())
        return {'system': system, 'groups': groups, 'coverage': coverage}

    def export_rows(self, kind, minutes):
        cutoff = time.time() - max(1, min(10080, int(minutes))) * 60
        table = 'group_samples' if kind == 'groups' else 'system_samples'
        with self.connect() as db:
            for row in db.execute(f'SELECT * FROM {table} WHERE ts>=? ORDER BY ts', (cutoff,)):
                yield dict(row)
