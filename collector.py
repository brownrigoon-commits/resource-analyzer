"""Read-only, bounded Windows resource collection. No model imports or inference."""
from __future__ import annotations
import collections
import os
from pathlib import Path
import re
import statistics
import time
import winreg
import psutil
from gpu_metrics import GpuSampler
from process_metrics import ProcessSampler
from temperature_metrics import TemperatureSampler

SAMPLE_INTERVAL_S = 1.0


def classify(name, exe='', command=()):
    # Arguments are inspected locally for identity only. Never retain/export them.
    n = name.lower()
    joined = ' '.join(command).lower().replace('/', '\\')
    path = exe.lower().replace('/', '\\')
    if 'resource-analyzer' in joined or n == 'resourceanalyzer.exe':
        return 'resource-analyzer', '자원 분석기', 'monitor'
    if 'all-games-marin-' in joined or 'my-sia-voice-studio' in joined:
        return 'my-sia-voice', 'MY SIA · 마린 음성 제작', 'voice'
    if 'music-studio' in joined:
        return 'music-studio', '뮤직 스튜디오', 'music'
    if re.search(r'\\rea\\|morluto[\\/]rea|\\rea(?:\.m?js|\.exe)(?:\s|$)', joined):
        return 'rea', 'REA · 프로그램 분석', 'analysis'
    if n in ('chatgpt.exe', 'codex.exe') or 'codex-computer-use' in n or n == 'extension-host.exe':
        return 'codex', 'Codex · ChatGPT', 'ai-client'
    if n == 'claude.exe' or ('claude' in path and n == 'node.exe'):
        return 'claude', 'Claude', 'ai-client'
    if n == 'chrome.exe':
        return 'chrome', 'Google Chrome', 'browser'
    if n in ('code.exe', 'code - insiders.exe'):
        return 'vscode', 'Visual Studio Code', 'development'
    if n in ('kakaotalk.exe', 'kakaotalkui.exe'):
        return 'kakao', '카카오톡', 'app'
    if n == 'explorer.exe':
        return 'explorer', 'Windows 탐색기', 'system'
    if n == 'taskmgr.exe':
        return 'task-manager', 'Windows 작업 관리자', 'system'
    if n == 'dwm.exe':
        return 'desktop-compositor', 'Windows 화면 합성', 'system'
    if n in ('system', 'registry', 'memory compression', 'secure system'):
        return 'windows-core', 'Windows 핵심 시스템', 'system'
    project = re.search(r'\\(?:rigoon_github|github|repos|projects)\\([^\\\s]+)', joined)
    if project:
        return 'project-' + project[1], '개발 · ' + project[1], 'development'
    return 'exe-' + n, name or '이름 확인 불가', 'process'


def aggregate_gpu(rows):
    """Sum process contributions to the same physical engine; then busiest engine."""
    engines = collections.defaultdict(float)
    for row in rows:
        for engine in row.get('engines', []):
            engines[engine['key']] += max(0.0, engine.get('pct', 0.0))
    if not engines:
        return 0.0, ''
    key = max(engines, key=engines.get)
    return round(min(100.0, engines[key]), 2), key


class Collector:
    def __init__(self):
        self.gpu = GpuSampler()
        self.process_sampler = ProcessSampler()
        self.temperatures = TemperatureSampler()
        self.previous = {}
        self.metadata = {}
        self.last_tick = time.monotonic()
        self.last_disk = psutil.disk_io_counters()
        self.last_partitions = 0
        self.disks = []
        self.cpu_threads = psutil.cpu_count() or 1
        self.cpu_name = 'CPU'
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                self.cpu_name = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
        except OSError:
            pass
        psutil.cpu_percent(percpu=True)
        self.sample()  # Prime cumulative CPU and I/O deltas before publication.

    def sample(self):
        now = time.monotonic()
        elapsed = max(.01, now - self.last_tick)
        ts = time.time()
        cpu_cores = psutil.cpu_percent(percpu=True)
        memory = psutil.virtual_memory()
        disk = psutil.disk_io_counters()
        read = write = 0.0
        if disk and self.last_disk:
            read = max(0, disk.read_bytes - self.last_disk.read_bytes) / elapsed / 1024**2
            write = max(0, disk.write_bytes - self.last_disk.write_bytes) / elapsed / 1024**2
        gpu_data = self.gpu.sample()
        gpu_processes = {int(k): v for k, v in gpu_data.get('processes', {}).items()}
        gpu_available = bool(gpu_data.get('processes_available', gpu_data.get('available')))
        processes = {}
        process_data = self.process_sampler.sample()
        readings = process_data.get('rows', []) if process_data.get('available') else self._fallback_processes(elapsed)
        current = set()
        denied = 0
        for raw in readings:
            pid = raw['pid']
            identity = (pid, raw.get('identity', raw.get('created_at')))
            current.add(identity)
            meta = self.metadata.get(identity)
            if meta is None:
                name, command, exe = raw['name'], [], ''
                try:
                    proc = psutil.Process(pid)
                    with proc.oneshot():
                        name, command, exe = proc.name(), proc.cmdline(), proc.exe()
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    denied += 1
                scripts = [Path(x).name for x in command if x.lower().endswith(('.py', '.js', '.mjs', '.ps1'))]
                meta = {'label': classify(name, exe, command), 'script': scripts[-1] if scripts else '', 'name': name}
                self.metadata[identity] = meta
            processes[pid] = {
                'pid': pid, 'ppid': raw.get('ppid'), 'name': meta['name'],
                'label': meta['label'], 'script': meta['script'],
                'cpu_pct': round(min(100, max(0, raw.get('cpu_pct') or 0)), 2),
                'ram_gb': round(raw.get('ram_gb') or 0, 4),
                'gpu_pct': gpu_processes.get(pid, {}).get('gpu_pct') if gpu_available else None,
                'io_read_mbps': round(raw.get('io_read_mbps') or 0, 3),
                'io_write_mbps': round(raw.get('io_write_mbps') or 0, 3),
            }
        # Only inherit explicit project/app identities; avoid treating every child
        # of explorer or a shell as the same application.
        inheritable = {'voice', 'music', 'analysis', 'monitor', 'ai-client', 'development'}
        for _ in range(5):
            for row in processes.values():
                parent = processes.get(row['ppid'])
                if parent and row['label'][2] == 'process' and parent['label'][2] in inheritable:
                    row['label'] = parent['label']
        groups = {}
        for row in processes.values():
            key, name, kind = row.pop('label')
            g = groups.setdefault(key, {'id': key, 'name': name, 'kind': kind, 'process_count': 0,
                                       'cpu_pct': 0.0, 'ram_gb': 0.0, 'gpu_pct': None,
                                       'gpu_engine': '', 'io_read_mbps': 0.0, 'io_write_mbps': 0.0,
                                       'pids': [], 'processes': []})
            g['process_count'] += 1
            for field in ('cpu_pct', 'ram_gb', 'io_read_mbps', 'io_write_mbps'):
                g[field] += row[field]
            g['pids'].append(row['pid'])
            g['processes'].append(row)
        for group in groups.values():
            if gpu_available and any(p in gpu_processes for p in group['pids']):
                group['gpu_pct'], group['gpu_engine'] = aggregate_gpu([gpu_processes.get(p, {}) for p in group['pids']])
            for field in ('cpu_pct', 'ram_gb', 'io_read_mbps', 'io_write_mbps'):
                group[field] = round(group[field], 3)
            group['processes'].sort(key=lambda p: (p['gpu_pct'] or 0, p['cpu_pct'], p['ram_gb']), reverse=True)
        if now - self.last_partitions > 60:
            self.disks = []
            for partition in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    self.disks.append({'drive': partition.device, 'label': partition.fstype,
                                       'total_gb': round(usage.total / 1024**3, 1),
                                       'free_gb': round(usage.free / 1024**3, 1), 'used_pct': usage.percent})
                except (OSError, PermissionError):
                    pass
            self.last_partitions = now
        self.metadata = {k: v for k, v in self.metadata.items() if k in current}
        self.last_tick, self.last_disk = now, disk
        return {'ts': ts, 'interval_s': SAMPLE_INTERVAL_S, 'system': {
            'cpu_pct': round(statistics.mean(cpu_cores), 2), 'cpu_threads': self.cpu_threads,
            'cpu_name': self.cpu_name, 'cpu_peak_core_pct': max(cpu_cores),
            'ram_used_gb': round((memory.total - memory.available) / 1024**3, 3),
            'ram_total_gb': round(memory.total / 1024**3, 3), 'ram_pct': memory.percent,
            'ram_available_gb': round(memory.available / 1024**3, 3), 'swap_pct': psutil.swap_memory().percent,
            'disk_read_mbps': round(read, 3), 'disk_write_mbps': round(write, 3),
            'disks': self.disks, 'gpus': gpu_data.get('gpus', []),
            'temperatures': self.temperatures.sample(),
        }, 'groups': sorted(groups.values(), key=lambda g: (g['gpu_pct'] or 0, g['cpu_pct'], g['ram_gb']), reverse=True),
            'errors': (gpu_data.get('errors', []) + process_data.get('errors', []))[:4],
            'coverage': {'readable_processes': len(processes), 'unreadable_processes': denied,
                         'process_gpu_available': gpu_available,
                         'process_bulk_available': bool(process_data.get('available'))}}

    def _fallback_processes(self, elapsed):
        rows, current = [], {}
        for proc in psutil.process_iter():
            if proc.pid == 0:
                continue
            try:
                with proc.oneshot():
                    created, name, ppid = proc.create_time(), proc.name(), proc.ppid()
                    times, mem = proc.cpu_times(), proc.memory_info().rss
                    try:
                        io = proc.io_counters()
                        read, write = io.read_bytes, io.write_bytes
                    except (psutil.AccessDenied, AttributeError):
                        read = write = 0
                identity = (proc.pid, created)
                cpu = times.user + times.system
                prev = self.previous.get(identity)
                current[identity] = (cpu, read, write)
                rows.append({'pid': proc.pid, 'ppid': ppid, 'name': name, 'identity': created,
                             'cpu_pct': max(0, (cpu-prev[0])/elapsed/self.cpu_threads*100) if prev else 0,
                             'ram_gb': mem/1024**3,
                             'io_read_mbps': max(0, read-prev[1])/elapsed/1024**2 if prev else 0,
                             'io_write_mbps': max(0, write-prev[2])/elapsed/1024**2 if prev else 0})
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                pass
        self.previous = current
        return rows

    def close(self):
        self.temperatures.close()
        self.process_sampler.close()
        self.gpu.close()
