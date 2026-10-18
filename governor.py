"""Conservative, cooperative admission control for local heavy job boundaries.

This module never starts, stops, moves, or changes another process. A lease is
owned by the *actual worker*, not by a launcher which can exit before its child.
Expiration is diagnostic: a living or inaccessible owner always keeps its slot.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

import psutil


THRESHOLDS = {
    'gpu_enter_pct': 60, 'gpu_exit_pct': 40, 'gpu_recovery_s': 5,
    'gpu_cpu_preference_s': 60,
    'cpu_max_pct': 70, 'ram_max_pct': 80, 'min_ram_available_gb': 6,
    'sample_max_age_s': 5, 'sample_max_duration_s': 3,
}
ADAPTERS = (
    {'stage': 'whisper-asr', 'label': 'Whisper 음성 인식',
     'devices': ['cpu', 'cuda'], 'default_device': 'cuda', 'cpu_verified': True},
    {'stage': 'cosyvoice-synthesis', 'label': 'CosyVoice 음성 합성',
     'devices': ['cuda'], 'default_device': 'cuda', 'cpu_verified': False},
    {'stage': 'audio-preprocess', 'label': '음성 전처리',
     'devices': ['cpu'], 'default_device': 'cpu', 'cpu_verified': True},
)
_ADAPTERS = {entry['stage']: entry for entry in ADAPTERS}
_COUNTERS = ('cpu_selected', 'completed', 'cpu_completed', 'deferred')
_HEARTBEAT_S = 30
_JOURNAL_LIMIT = 64 * 1024


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _owner_state(pid, created):
    """Return alive/dead/unknown; denied access must never free a lease."""
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) > .001:
            return 'dead'  # The PID was reused; the original worker has exited.
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return 'dead'
        return 'alive'
    except psutil.NoSuchProcess:
        return 'dead'
    except (psutil.AccessDenied, OSError):
        return 'unknown'


def _identity(payload, require_lease=False):
    if not isinstance(payload, dict):
        raise ValueError('요청은 JSON 객체여야 합니다.')
    stage = payload.get('stage')
    if not isinstance(stage, str) or stage not in _ADAPTERS:
        raise ValueError('중앙 정책이 지원하는 작업 단계가 아닙니다.')
    pid, created = payload.get('owner_pid'), payload.get('owner_created')
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 0xffffffff:
        raise ValueError('실제 작업 프로세스 ID가 필요합니다.')
    if not _number(created) or created <= 0:
        raise ValueError('실제 작업 프로세스 생성 시각이 필요합니다.')
    request_id = payload.get('request_id')
    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
            or any(not (char.isascii() and (char.isalnum() or char in '-_.')) for char in request_id)):
        raise ValueError('유효한 작업 요청 ID가 필요합니다.')
    result = {'stage': stage, 'owner_pid': pid, 'owner_created': float(created),
              'request_id': request_id}
    if require_lease:
        lease_id = payload.get('lease_id')
        if not isinstance(lease_id, str) or not 24 <= len(lease_id) <= 128:
            raise ValueError('유효한 작업 임대 토큰이 필요합니다.')
        result['lease_id'] = lease_id
    return result


def _same_request(first, second):
    return all(first.get(key) == second.get(key)
               for key in ('stage', 'owner_pid', 'owner_created', 'request_id'))


def _same_lease(first, second):
    return (_same_request(first, second)
            and secrets.compare_digest(first.get('lease_id', ''), second.get('lease_id', '')))


class Governor:
    def __init__(self, data_dir):
        self._lock = threading.RLock()
        self._path = Path(data_dir) / 'governor.json'
        self._enabled = False
        self._active = None
        self._last_release = None
        self._stats = dict.fromkeys(_COUNTERS, 0)
        self._journal_error = None
        self._sample = None
        self._observed_at = None
        self._sample_error = '아직 정상 측정값을 받지 못했습니다.'
        self._recovery_required = False
        self._healthy_since = None
        self._pressure = False
        self._gpu_low_since = None
        # Keep CPU-capable follow-up stages on CPU across model teardown and
        # worker startup gaps. These monotonic observations are intentionally
        # not persisted as wall-clock history in the lease journal.
        self._last_gpu_high_at = None
        self._last_cpu_completion_at = None
        self._last_touch = None
        self._recovered_lease = False
        self._owner_lock = None
        self._claim_authority()
        self._load()

    def _claim_authority(self):
        """One governor per state directory, including servers on other ports."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock = (self._path.parent / 'governor.lock').open('a+b')
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._owner_lock = lock
        except OSError:
            if 'lock' in locals():
                lock.close()
            self._journal_error = '같은 기록 폴더의 중앙 정책이 이미 실행 중이거나 잠금을 확인할 수 없습니다.'

    def close(self):
        """Release server authority without clearing a possibly living worker."""
        with self._lock:
            if self._owner_lock is not None:
                self._owner_lock.close()
                self._owner_lock = None
            self._journal_error = '중앙 정책 서비스가 종료되어 새 작업을 보류합니다.'

    def _load(self):
        if self._journal_error:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if not self._path.exists():
                return
            if self._path.stat().st_size > _JOURNAL_LIMIT:
                raise ValueError('journal too large')
            value = json.loads(self._path.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or value.get('version') != 1
                    or not isinstance(value.get('enabled'), bool)):
                raise ValueError('invalid journal version or policy')
            stats = value.get('stats')
            if (not isinstance(stats, dict) or any(
                    not isinstance(stats.get(key), int) or isinstance(stats.get(key), bool)
                    or not 0 <= stats[key] <= 10**15 for key in _COUNTERS)):
                raise ValueError('invalid journal statistics')
            active = value.get('active')
            if active is not None:
                identity = _identity(active, require_lease=True)
                if (active.get('device') not in _ADAPTERS[identity['stage']]['devices']
                        or not _number(active.get('acquired_at'))
                        or not _number(active.get('last_seen_at'))):
                    raise ValueError('invalid active lease')
            released = value.get('last_release')
            if released is not None:
                _identity(released, require_lease=True)
            self._enabled, self._stats = value['enabled'], {key: stats[key] for key in _COUNTERS}
            self._active, self._last_release = active, released
            self._recovered_lease = active is not None
        except (OSError, ValueError, TypeError, KeyError):
            self._journal_error = '작업 임대 기록을 확인할 수 없어 새 작업을 보류합니다.'

    def _save(self, *, enabled=None, active=..., last_release=..., stats=None):
        """Persist before granting/releasing; an uncertain write fails closed."""
        if self._journal_error:
            return False
        value = {'version': 1, 'enabled': self._enabled if enabled is None else enabled,
                 'active': self._active if active is ... else active,
                 'last_release': self._last_release if last_release is ... else last_release,
                 'stats': self._stats if stats is None else stats}
        temporary = self._path.with_name(self._path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            return True
        except (OSError, ValueError, TypeError):
            self._journal_error = '작업 임대 기록 저장에 실패하여 새 작업을 보류합니다.'
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def invalidate(self, reason):
        with self._lock:
            self._sample_error = str(reason or '측정이 중단되었습니다.')[:300]
            self._sample = None
            self._healthy_since = None
            self._gpu_low_since = None
            self._recovery_required = True

    def update(self, sample, observed_at=None):
        with self._lock:
            now = time.monotonic()
            observed_at = now if observed_at is None else observed_at
            try:
                if not isinstance(sample, dict) or not isinstance(sample.get('system'), dict):
                    raise ValueError('정상 시스템 측정값이 필요합니다.')
                if sample.get('errors') or sample.get('error') or sample.get('collecting') is False:
                    raise ValueError('측정 오류 또는 수집 중단으로 새 작업을 보류합니다.')
                duration = sample.get('collection_duration_s')
                if not _number(duration) or not 0 <= duration <= THRESHOLDS['sample_max_duration_s']:
                    raise ValueError('측정 시간이 길거나 확인되지 않아 새 작업을 보류합니다.')
                if (not _number(observed_at) or observed_at > now + .01
                        or now - observed_at > THRESHOLDS['sample_max_age_s']):
                    raise ValueError('측정값이 오래되어 새 작업을 보류합니다.')
                system = sample['system']
                cpu, ram, free = (system.get(key) for key in ('cpu_pct', 'ram_pct', 'ram_available_gb'))
                if (not _number(cpu) or not 0 <= cpu <= 100 or not _number(ram)
                        or not 0 <= ram <= 100 or not _number(free) or free < 0):
                    raise ValueError('CPU 또는 RAM 측정값을 확인할 수 없습니다.')
                gpus = system.get('gpus')
                if not isinstance(gpus, list) or not gpus:
                    raise ValueError('GPU 전체 사용률을 확인할 수 없습니다.')
                totals = []
                for gpu in gpus:
                    if not isinstance(gpu, dict) or gpu.get('source') not in ('NVML', 'nvidia-smi'):
                        raise ValueError('지원되는 GPU 전체 측정값이 필요합니다.')
                    util = gpu.get('util_pct')
                    if not _number(util) or not 0 <= util <= 100:
                        raise ValueError('GPU 전체 사용률이 유효하지 않습니다.')
                    totals.append(float(util))
                previous = self._observed_at
                if previous is not None and now - previous > THRESHOLDS['sample_max_age_s']:
                    self._recovery_required = True
                    self._healthy_since = None
                self._sample = {'cpu_pct': float(cpu), 'ram_pct': float(ram),
                                'ram_available_gb': float(free), 'gpu_pct': max(totals)}
                self._sample_error = None
                self._observed_at = float(observed_at)
                if self._healthy_since is None:
                    self._healthy_since = observed_at
                if self._sample['gpu_pct'] >= THRESHOLDS['gpu_enter_pct']:
                    self._last_gpu_high_at = float(observed_at)
                    self._pressure, self._gpu_low_since = True, None
                elif self._pressure:
                    if self._sample['gpu_pct'] <= THRESHOLDS['gpu_exit_pct']:
                        if self._gpu_low_since is None:
                            self._gpu_low_since = observed_at
                        if observed_at - self._gpu_low_since >= THRESHOLDS['gpu_recovery_s']:
                            self._pressure, self._gpu_low_since = False, None
                    else:
                        self._gpu_low_since = None
            except (ValueError, TypeError, KeyError) as error:
                self.invalidate(str(error))

    def configure(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('정책 사용 여부는 true 또는 false여야 합니다.')
        with self._lock:
            if self._save(enabled=enabled):
                self._enabled = enabled
            return self._status()

    def _guard(self):
        if self._journal_error:
            return self._journal_error
        if self._sample_error or self._sample is None or self._observed_at is None:
            return self._sample_error or '정상 측정값을 기다리고 있습니다.'
        now = time.monotonic()
        if now - self._observed_at > THRESHOLDS['sample_max_age_s'] or now < self._observed_at:
            self._recovery_required, self._healthy_since = True, None
            return '측정값이 오래되어 새 작업을 보류합니다.'
        if self._recovery_required:
            if (self._healthy_since is None
                    or self._observed_at - self._healthy_since < THRESHOLDS['gpu_recovery_s']):
                return '측정 복구 후 5초 동안 안정 상태를 확인하고 있습니다.'
            self._recovery_required = False
        if self._sample['ram_pct'] >= THRESHOLDS['ram_max_pct']:
            return 'RAM 사용률이 80% 이상이어서 새 작업을 보류합니다.'
        if self._sample['ram_available_gb'] < THRESHOLDS['min_ram_available_gb']:
            return '사용 가능한 RAM이 6GiB 미만이어서 새 작업을 보류합니다.'
        if self._sample['cpu_pct'] >= THRESHOLDS['cpu_max_pct']:
            return 'CPU 사용률이 70% 이상이어서 새 작업을 보류합니다.'
        return None

    def _reconcile(self):
        if self._active and _owner_state(self._active['owner_pid'], self._active['owner_created']) == 'dead':
            if self._save(active=None):
                self._active, self._last_touch, self._recovered_lease = None, None, False

    def _orphaned(self):
        if not self._active:
            return False
        return (self._recovered_lease or self._last_touch is None
                or time.monotonic() - self._last_touch > _HEARTBEAT_S
                or _owner_state(self._active['owner_pid'], self._active['owner_created']) != 'alive')

    def _wait(self, reason, count=True):
        if count:
            self._stats['deferred'] += 1
        return {'decision': 'wait', 'reason': reason, 'retry_after_s': 1, 'threads': self._threads()}

    @staticmethod
    def _threads():
        return max(1, min(8, max(1, (os.cpu_count() or 2) // 2)))

    def _grant(self, lease):
        return {'decision': lease['device'], 'reason': '중앙 정책이 다음 작업을 허용했습니다.',
                'retry_after_s': 0, 'threads': self._threads(), 'lease_id': lease['lease_id']}

    def _existing_decision(self, lease):
        reason = self._guard()
        if reason:
            return self._wait(reason)
        if self._enabled and self._pressure and lease['device'] == 'cuda':
            return self._wait('GPU 사용률이 높아 현재 구간을 마친 뒤 다음 구간을 기다립니다.')
        return self._grant(lease)

    def _cpu_preference(self):
        now = time.monotonic()
        observations = [stamp for stamp in (self._last_gpu_high_at, self._last_cpu_completion_at)
                        if stamp is not None and stamp <= now]
        if not observations:
            return False, 0.0
        age = now - max(observations)
        duration = THRESHOLDS['gpu_cpu_preference_s']
        return age <= duration, max(0.0, duration - age)

    def acquire(self, payload):
        identity = _identity(payload)
        with self._lock:
            if _owner_state(identity['owner_pid'], identity['owner_created']) != 'alive':
                raise PermissionError('실제 작업 프로세스의 소유권을 확인할 수 없습니다.')
            self._reconcile()
            if self._active:
                if _same_request(self._active, identity):
                    return self._existing_decision(self._active)
                if self._orphaned():
                    return self._wait('기존 작업의 종료가 확인되지 않아 임대 슬롯을 유지합니다.')
                return self._wait('다른 무거운 작업이 실행 중이어서 차례를 기다립니다.')
            if self._last_release and _same_request(self._last_release, identity):
                raise PermissionError('이미 반환한 작업 요청 ID를 다시 사용할 수 없습니다.')
            reason = self._guard()
            if reason:
                return self._wait(reason)
            adapter = _ADAPTERS[identity['stage']]
            device = adapter['default_device']
            if self._enabled and self._pressure:
                if 'cpu' not in adapter['devices']:
                    return self._wait('GPU 사용률이 60% 이상이며 이 단계는 CPU 분배를 지원하지 않습니다.')
                device = 'cpu'
            elif (self._enabled and adapter['default_device'] == 'cuda'
                  and 'cpu' in adapter['devices'] and self._cpu_preference()[0]):
                device = 'cpu'
            now = time.time()
            lease = {**identity, 'lease_id': secrets.token_urlsafe(32), 'device': device,
                     'acquired_at': now, 'last_seen_at': now}
            stats = dict(self._stats)
            if device == 'cpu':
                stats['cpu_selected'] += 1
            if not self._save(active=lease, stats=stats):
                return self._wait(self._journal_error)
            self._active, self._stats = lease, stats
            self._last_touch, self._recovered_lease = time.monotonic(), False
            return self._grant(lease)

    def checkpoint(self, payload):
        identity = _identity(payload, require_lease=True)
        with self._lock:
            if not self._active or not _same_lease(self._active, identity):
                raise PermissionError('작업 임대 토큰 또는 소유자가 일치하지 않습니다.')
            if _owner_state(identity['owner_pid'], identity['owner_created']) != 'alive':
                raise PermissionError('실제 작업 프로세스의 소유권을 확인할 수 없습니다.')
            lease = {**self._active, 'last_seen_at': time.time()}
            if not self._save(active=lease):
                return self._wait(self._journal_error)
            self._active, self._last_touch = lease, time.monotonic()
            self._recovered_lease = False
            return self._existing_decision(lease)

    def release(self, payload):
        identity = _identity(payload, require_lease=True)
        completed = payload.get('completed', False)
        if not isinstance(completed, bool):
            raise ValueError('완료 여부는 true 또는 false여야 합니다.')
        with self._lock:
            if _owner_state(identity['owner_pid'], identity['owner_created']) != 'alive':
                raise PermissionError('실제 작업 프로세스의 소유권을 확인할 수 없습니다.')
            if self._last_release and _same_lease(self._last_release, identity):
                return {**self._wait('이미 반환한 작업 임대입니다.', count=False), 'released': True}
            if not self._active or not _same_lease(self._active, identity):
                raise PermissionError('작업 임대 토큰 또는 소유자가 일치하지 않습니다.')
            stats = dict(self._stats)
            extend_cpu_preference = (completed and self._active['device'] == 'cpu'
                                     and _ADAPTERS[self._active['stage']]['default_device'] == 'cuda')
            if completed:
                stats['completed'] += 1
                if self._active['device'] == 'cpu':
                    stats['cpu_completed'] += 1
            if not self._save(active=None, last_release=identity, stats=stats):
                return {**self._wait(self._journal_error), 'released': False}
            self._active, self._last_touch, self._recovered_lease = None, None, False
            self._last_release, self._stats = identity, stats
            if extend_cpu_preference:
                self._last_cpu_completion_at = time.monotonic()
            return {**self._wait('작업 임대를 반환했습니다.', count=False), 'released': True}

    def _status(self):
        self._reconcile()
        reason = self._guard()
        cpu_preferred, cpu_preference_remaining = self._cpu_preference()
        active = None
        orphaned = self._orphaned()
        if self._active:
            active = {key: self._active[key] for key in
                      ('stage', 'device', 'owner_pid', 'owner_created', 'request_id')}
            active.update(age_s=round(max(0, time.time() - self._active['acquired_at']), 2),
                          heartbeat_age_s=(round(max(0, time.monotonic() - self._last_touch), 2)
                                           if self._last_touch is not None else None),
                          orphaned=orphaned)
        if self._journal_error:
            mode = 'fault'
        elif orphaned:
            mode, reason = 'orphaned', '기존 작업의 종료가 확인되지 않아 임대 슬롯을 유지합니다.'
        elif reason:
            mode = 'waiting'
        elif active:
            mode, reason = 'active', '연동된 무거운 작업이 실행 중입니다.'
        elif not self._enabled:
            mode, reason = 'disabled', '자동 CPU 분배가 꺼져 있습니다. 기본 장치와 안전 보호를 사용합니다.'
        elif self._pressure:
            mode, reason = 'gpu-pressure', 'GPU 사용률이 높아 지원되는 다음 작업을 CPU로 분배합니다.'
        elif cpu_preferred:
            mode, reason = 'cpu-preference', '최근 GPU 부하 또는 CPU 작업 완료 기록에 따라 다음 지원 작업을 CPU로 분배합니다.'
        else:
            mode, reason = 'ready', '다음 연동 작업을 기다리고 있습니다.'
        return {'enabled': self._enabled, 'mode': mode, 'thresholds': dict(THRESHOLDS),
                'active': active, 'reason': reason, 'adapters': copy.deepcopy(list(ADAPTERS)),
                'cpu_preference_remaining_s': round(cpu_preference_remaining, 2),
                'stats': dict(self._stats)}

    def status(self):
        with self._lock:
            return self._status()
