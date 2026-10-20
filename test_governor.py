from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import psutil

import governor
from governor import Governor
import resource_client
from resource_client import ResourceSession


def sample(gpu=20, cpu=10, ram=30, free=20):
    return {'collection_duration_s': .1, 'errors': [], 'system': {
        'cpu_pct': cpu, 'ram_pct': ram, 'ram_available_gb': free,
        'gpus': [{'id': 'gpu-0', 'source': 'NVML', 'util_pct': gpu}]}}


def request(ident='job-1', pid=100, created=10, stage='whisper-asr', lease=None):
    value = {'stage': stage, 'owner_pid': pid, 'owner_created': created, 'request_id': ident}
    if lease is not None:
        value['lease_id'] = lease
    return value


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 1000.0
        self.clock = patch('governor.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.owner = patch('governor._owner_state', return_value='alive')
        self.owner.start()
        self.addCleanup(self.owner.stop)
        self.gov = Governor(self.folder.name)
        self.addCleanup(self.gov.close)
        self.gov.update(sample(), observed_at=self.now)

    def update(self, **changes):
        self.gov.update(sample(**changes), observed_at=self.now)

    def advance(self, seconds, gpu=20):
        target = self.now + seconds
        while self.now < target:
            self.now = min(target, self.now + 1)
            self.update(gpu=gpu)

    def release(self, grant, ident='job-1', completed=False, stage='whisper-asr'):
        return self.gov.release({**request(ident, stage=stage, lease=grant['lease_id']), 'completed': completed})

    def test_policy_defaults_off_but_guards_remain(self):
        self.assertFalse(self.gov.status()['enabled'])
        self.update(gpu=99)
        grant = self.gov.acquire(request())
        self.assertEqual(grant['decision'], 'cuda')
        self.release(grant)
        self.update(cpu=70)
        self.assertEqual(self.gov.acquire(request('job-2'))['decision'], 'wait')

    def test_qwen_enabled_uses_shared_vision_and_cpu_summary(self):
        self.gov.configure(True)
        grant = self.gov.acquire(request(stage='qwen-vision'))
        self.assertEqual((grant['decision'], grant['gpu_layers']), ('hybrid', 8))
        self.release(grant, stage='qwen-vision', completed=True)
        grant = self.gov.acquire(request('summary', stage='qwen-summary'))
        self.assertEqual((grant['decision'], grant['gpu_layers']), ('cpu', 0))
        self.release(grant, 'summary', stage='qwen-summary', completed=True)
        self.assertEqual(self.gov.status()['stats']['cpu_completed'], 2)

    def test_qwen_busy_gpu_uses_cpu_and_ram_guard_still_applies(self):
        self.gov.configure(True)
        self.update(gpu=99)
        grant = self.gov.acquire(request(stage='qwen-vision'))
        self.assertEqual(grant['decision'], 'cpu')
        self.release(grant, stage='qwen-vision')
        self.update(ram=81)
        self.assertEqual(self.gov.acquire(request('blocked', stage='qwen-vision'))['decision'], 'wait')

    def test_qwen_disabled_uses_full_gpu(self):
        grant = self.gov.acquire(request(stage='qwen-vision'))
        self.assertEqual((grant['decision'], grant['gpu_layers']), ('cuda', -1))
        self.release(grant, stage='qwen-vision')

    def test_gpu_threshold_and_five_second_hysteresis(self):
        self.gov.configure(True)
        self.update(gpu=59.9)
        grant = self.gov.acquire(request())
        self.assertEqual(grant['decision'], 'cuda')
        self.release(grant)
        self.update(gpu=60)
        grant = self.gov.acquire(request('job-2'))
        self.assertEqual(grant['decision'], 'cpu')
        self.release(grant, 'job-2')
        self.now += 1
        self.update(gpu=40)
        self.now += 4
        self.update(gpu=40.1)  # Resets recovery; 40.1 is not the exit boundary.
        self.now += 1
        self.update(gpu=40)
        for _ in range(4):
            self.now += 1
            self.update(gpu=40)
        self.assertEqual(self.gov.status()['mode'], 'gpu-pressure')
        self.now += 1
        self.update(gpu=40)
        self.assertEqual(self.gov.status()['mode'], 'cpu-preference')
        grant = self.gov.acquire(request('job-3'))
        self.assertEqual(grant['decision'], 'cpu')
        self.release(grant, 'job-3')
        self.assertEqual(self.gov.acquire(request('job-4', stage='cosyvoice-synthesis'))['decision'], 'cuda')

    def test_cpu_preference_expires_after_sixty_seconds_without_delaying_gpu_only_work(self):
        self.gov.configure(True)
        self.update(gpu=60)
        self.advance(6)
        self.assertEqual(self.gov.status()['cpu_preference_remaining_s'], 54)
        synthesis = self.gov.acquire(request('synthesis', stage='cosyvoice-synthesis'))
        self.assertEqual(synthesis['decision'], 'cuda')
        self.release(synthesis, 'synthesis', stage='cosyvoice-synthesis', completed=True)
        self.advance(54)
        grant = self.gov.acquire(request('at-sixty'))
        self.assertEqual(grant['decision'], 'cpu')
        self.release(grant, 'at-sixty')
        self.advance(.001)
        self.assertEqual(self.gov.status()['mode'], 'ready')
        self.assertEqual(self.gov.acquire(request('after-sixty'))['decision'], 'cuda')

    def test_a_new_real_gpu_high_observation_refreshes_cpu_preference(self):
        self.gov.configure(True)
        self.update(gpu=60)
        self.advance(40)
        self.update(gpu=60)
        self.advance(30)
        self.assertEqual(self.gov.status()['cpu_preference_remaining_s'], 30)
        self.assertEqual(self.gov.acquire(request())['decision'], 'cpu')

    def test_successful_cpu_asr_completion_extends_preference_for_follow_up_qa(self):
        self.gov.configure(True)
        self.update(gpu=60)
        self.advance(50)
        first = self.gov.acquire(request('first-qa'))
        self.assertEqual(first['decision'], 'cpu')
        self.advance(20)
        self.assertEqual(self.gov.checkpoint(request('first-qa', lease=first['lease_id']))['decision'], 'cpu')
        self.release(first, 'first-qa', completed=True)
        self.assertEqual(self.gov.status()['cpu_preference_remaining_s'], 60)
        self.advance(59)
        second = self.gov.acquire(request('qa-medium'))
        self.assertEqual(second['decision'], 'cpu')
        self.release(second, 'qa-medium', completed=False)
        self.advance(1.001)
        self.assertEqual(self.gov.acquire(request('later-qa'))['decision'], 'cuda')

    def test_cpu_only_preprocess_does_not_start_or_extend_cpu_preference(self):
        self.gov.configure(True)
        prepare = self.gov.acquire(request('prepare-only', stage='audio-preprocess'))
        self.release(prepare, 'prepare-only', stage='audio-preprocess', completed=True)
        self.assertEqual(self.gov.status()['cpu_preference_remaining_s'], 0)
        self.assertEqual(self.gov.status()['mode'], 'ready')
        self.update(gpu=60)
        self.advance(50)
        prepare = self.gov.acquire(request('prepare-after-high', stage='audio-preprocess'))
        self.release(prepare, 'prepare-after-high', stage='audio-preprocess', completed=True)
        self.assertEqual(self.gov.status()['cpu_preference_remaining_s'], 10)
        self.advance(10.001)
        self.assertEqual(self.gov.acquire(request('asr-after-prepare'))['decision'], 'cuda')

    def test_repeated_completion_response_does_not_extend_preference_again(self):
        self.gov.configure(True)
        self.update(gpu=60)
        grant = self.gov.acquire(request())
        self.advance(6)
        self.release(grant, completed=True)
        self.advance(61)
        self.release(grant, completed=True)
        self.assertEqual(self.gov.status()['cpu_preference_remaining_s'], 0)
        self.assertEqual(self.gov.acquire(request('next-job'))['decision'], 'cuda')

    def test_cpu_preference_respects_guards_and_policy_disabled(self):
        self.gov.configure(True)
        self.update(gpu=60)
        self.advance(6)
        for changes in ({'cpu': 70}, {'ram': 80}, {'free': 5.999}):
            with self.subTest(changes=changes):
                self.update(**changes)
                self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
        self.gov.invalidate('측정 중단')
        self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
        self.update()
        self.advance(5)
        self.assertEqual(self.gov.status()['mode'], 'cpu-preference')
        self.now += 5.001
        self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
        self.update()
        self.advance(5)
        self.gov.configure(False)
        self.assertEqual(self.gov.acquire(request())['decision'], 'cuda')

    def test_restarted_cpu_asr_lease_extends_preference_without_changing_journal_schema(self):
        self.gov.configure(True)
        self.update(gpu=60)
        grant = self.gov.acquire(request())
        self.gov.close()
        restored = Governor(self.folder.name)
        self.addCleanup(restored.close)
        restored.update(sample(), observed_at=self.now)
        self.assertEqual(restored.status()['cpu_preference_remaining_s'], 0)
        self.assertEqual(restored.checkpoint(request(lease=grant['lease_id']))['decision'], 'cpu')
        restored.release({**request(lease=grant['lease_id']), 'completed': True})
        self.assertEqual(restored.status()['cpu_preference_remaining_s'], 60)
        self.assertEqual(restored.acquire(request('next-qa'))['decision'], 'cpu')
        journal = json.loads((Path(self.folder.name) / 'governor.json').read_text(encoding='utf-8'))
        self.assertEqual(set(journal), {'version', 'enabled', 'active', 'last_release', 'stats'})
        self.assertEqual(journal['version'], 1)

    def test_cpu_ram_and_minimum_free_boundaries(self):
        for changes in ({'cpu': 70}, {'ram': 80}, {'free': 5.999}):
            with self.subTest(changes=changes):
                self.update(**changes)
                self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
        self.update(cpu=69.999, ram=79.999, free=6)
        self.assertEqual(self.gov.acquire(request())['decision'], 'cuda')

    def test_gpu_only_and_cpu_only_adapters(self):
        self.gov.configure(True)
        self.update(gpu=60)
        self.assertEqual(self.gov.acquire(request(stage='cosyvoice-synthesis'))['decision'], 'wait')
        result = self.gov.acquire(request(stage='audio-preprocess'))
        self.assertEqual(result['decision'], 'cpu')
        self.assertLessEqual(result['threads'], 8)

    def test_twenty_concurrent_requests_get_one_lease(self):
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(lambda index: self.gov.acquire(request('job-%s' % index)), range(20)))
        self.assertEqual(sum(result['decision'] != 'wait' for result in results), 1)
        self.assertEqual(self.gov.status()['stats']['deferred'], 19)

    def test_acquire_is_idempotent_and_completed_request_not_reused(self):
        grant = self.gov.acquire(request(stage='audio-preprocess'))
        duplicate = self.gov.acquire(request(stage='audio-preprocess'))
        self.assertEqual(grant['lease_id'], duplicate['lease_id'])
        self.assertEqual(self.gov.status()['stats']['cpu_selected'], 1)
        self.release(grant, stage='audio-preprocess')
        with self.assertRaises(PermissionError):
            self.gov.acquire(request(stage='audio-preprocess'))

    def test_invalid_measurements_fail_closed(self):
        cases = []
        for field in ('cpu_pct', 'ram_pct', 'ram_available_gb'):
            for value in (None, float('nan'), float('inf'), True, -1):
                item = sample()
                item['system'][field] = value
                cases.append(item)
        for value in (None, float('nan'), float('inf'), 101, -1):
            item = sample()
            item['system']['gpus'][0]['util_pct'] = value
            cases.append(item)
        item = sample()
        item['system']['gpus'][0]['source'] = 'Windows GPU Engine'
        cases.append(item)
        item = sample()
        item['system']['gpus'] = []
        cases.append(item)
        item = sample()
        del item['system']['ram_available_gb']
        cases.append(item)
        for value in (3.001, None, float('nan')):
            item = sample()
            item['collection_duration_s'] = value
            cases.append(item)
        for item in cases:
            with self.subTest(item=item):
                self.gov.update(item, observed_at=self.now)
                self.assertEqual(self.gov.acquire(request())['decision'], 'wait')

    def test_observation_must_include_actual_collection_age(self):
        self.gov.update(sample(), observed_at=self.now - 5.001)
        self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
        self.gov.update(sample(), observed_at=self.now + 1)
        self.assertEqual(self.gov.acquire(request())['decision'], 'wait')

    def test_pause_and_error_recover_only_after_five_seconds_of_new_samples(self):
        for invalidate in ('pause', 'error'):
            with self.subTest(invalidate=invalidate):
                if invalidate == 'pause':
                    self.gov.invalidate('수집을 정지했습니다.')
                else:
                    item = sample()
                    item['errors'] = ['counter failed']
                    self.gov.update(item, observed_at=self.now)
                self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
                self.update()
                for _ in range(4):
                    self.now += 1
                    self.update()
                    self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
                self.now += 1
                self.update()
                grant = self.gov.acquire(request('recovered-' + invalidate))
                self.assertEqual(grant['decision'], 'cuda')
                self.release(grant, 'recovered-' + invalidate)

    def test_stale_and_repeated_old_sample_do_not_clear_recovery(self):
        self.now += 5.001
        self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
        self.update()
        self.now += 5
        self.assertEqual(self.gov.acquire(request())['decision'], 'wait')
        self.update()
        self.assertEqual(self.gov.acquire(request())['decision'], 'cuda')

    def test_cuda_checkpoint_waits_without_switching_live_device(self):
        self.gov.configure(True)
        grant = self.gov.acquire(request())
        self.update(gpu=60)
        response = self.gov.checkpoint(request(lease=grant['lease_id']))
        self.assertEqual(response['decision'], 'wait')
        self.assertEqual(self.gov.status()['active']['device'], 'cuda')
        self.assertIsNotNone(self.gov.status()['active'])
        self.advance(6)
        self.assertEqual(self.gov.checkpoint(request(lease=grant['lease_id']))['decision'], 'cuda')
        self.assertGreater(self.gov.status()['cpu_preference_remaining_s'], 0)

    def test_cpu_checkpoint_keeps_device_and_cpu_guard_applies_to_cuda(self):
        self.gov.configure(True)
        self.update(gpu=60)
        grant = self.gov.acquire(request())
        for _ in range(6):
            self.now += 1
            self.update(gpu=20)
        self.assertEqual(self.gov.checkpoint(request(lease=grant['lease_id']))['decision'], 'cpu')
        self.release(grant)
        grant = self.gov.acquire(request('job-2', stage='cosyvoice-synthesis'))
        self.assertEqual(grant['decision'], 'cuda')
        self.update(cpu=70)
        self.assertEqual(self.gov.checkpoint(request('job-2', stage='cosyvoice-synthesis', lease=grant['lease_id']))['decision'], 'wait')

    def test_wrong_owner_and_token_cannot_release_or_checkpoint(self):
        grant = self.gov.acquire(request())
        for invalid in (request(lease='a' * 43), request(pid=101, lease=grant['lease_id']),
                        request(created=11, lease=grant['lease_id'])):
            for operation in (self.gov.release, self.gov.checkpoint):
                with self.subTest(invalid=invalid, operation=operation):
                    with self.assertRaises(PermissionError):
                        operation(invalid)
        self.assertIsNotNone(self.gov.status()['active'])

    def test_alive_expired_lease_is_never_reassigned(self):
        grant = self.gov.acquire(request())
        self.now += 31
        self.update()
        self.assertEqual(self.gov.acquire(request('job-2'))['decision'], 'wait')
        self.assertTrue(self.gov.status()['active']['orphaned'])
        self.gov.checkpoint(request(lease=grant['lease_id']))
        self.assertFalse(self.gov.status()['active']['orphaned'])

    def test_unknown_owner_is_not_reclaimed(self):
        self.gov.acquire(request())
        with patch('governor._owner_state', side_effect=lambda pid, created: 'unknown' if pid == 100 else 'alive'):
            self.assertEqual(self.gov.acquire(request('job-2', pid=101))['decision'], 'wait')
            self.assertIsNotNone(self.gov.status()['active'])

    def test_pid_reuse_reclaims_only_the_exited_original(self):
        self.gov.acquire(request())
        with patch('governor._owner_state', side_effect=lambda pid, created: 'dead' if created == 10 else 'alive'):
            result = self.gov.acquire(request('job-2', created=20))
        self.assertEqual(result['decision'], 'cuda')
        self.assertEqual(self.gov.status()['active']['owner_created'], 20)
        self.assertEqual(self.gov.status()['stats']['completed'], 0)

    def test_journal_recovers_a_living_lease(self):
        self.gov.configure(True)
        grant = self.gov.acquire(request())
        self.gov.close()
        restored = Governor(self.folder.name)
        self.addCleanup(restored.close)
        restored.update(sample(), observed_at=self.now)
        self.assertEqual(restored.status()['mode'], 'orphaned')
        self.assertEqual(restored.acquire(request('job-2'))['decision'], 'wait')
        self.assertEqual(restored.checkpoint(request(lease=grant['lease_id']))['decision'], 'cuda')
        self.assertTrue(restored.release({**request(lease=grant['lease_id']), 'completed': True})['released'])
        self.assertTrue(restored.status()['enabled'])

    def test_second_governor_cannot_serve_same_state_folder(self):
        other = Governor(self.folder.name)
        self.addCleanup(other.close)
        other.update(sample(), observed_at=self.now)
        self.assertEqual(other.status()['mode'], 'fault')
        self.assertEqual(other.acquire(request())['decision'], 'wait')

    def test_corrupt_journal_is_not_overwritten_or_ignored(self):
        self.gov.close()
        path = Path(self.folder.name) / 'governor.json'
        path.write_text('{broken', encoding='utf-8')
        restored = Governor(self.folder.name)
        self.addCleanup(restored.close)
        restored.update(sample(), observed_at=self.now)
        self.assertEqual(restored.configure(True)['mode'], 'fault')
        self.assertEqual(restored.acquire(request())['decision'], 'wait')
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_failed_atomic_write_never_grants(self):
        with patch('governor.os.replace', side_effect=OSError('disk full')):
            result = self.gov.acquire(request())
        self.assertEqual(result['decision'], 'wait')
        self.assertEqual(self.gov.status()['mode'], 'fault')
        self.assertIsNone(self.gov.status()['active'])

    def test_completion_is_explicit_and_release_is_idempotent(self):
        grant = self.gov.acquire(request(stage='audio-preprocess'))
        self.release(grant, stage='audio-preprocess', completed=True)
        self.release(grant, stage='audio-preprocess', completed=True)
        stats = self.gov.status()['stats']
        self.assertEqual((stats['cpu_selected'], stats['completed'], stats['cpu_completed']), (1, 1, 1))
        grant = self.gov.acquire(request('job-2', stage='audio-preprocess'))
        self.release(grant, 'job-2', stage='audio-preprocess', completed=False)
        self.assertEqual(self.gov.status()['stats']['completed'], 1)

    def test_status_never_exposes_lease_capability(self):
        grant = self.gov.acquire(request())
        self.assertNotIn(grant['lease_id'], json.dumps(self.gov.status()))

    def test_failed_worker_session_blocks_new_work_until_owner_exits(self):
        client = ResourceSession('whisper-asr')
        with patch.object(client, '_post', side_effect=lambda operation, payload: getattr(self.gov, operation)(payload)):
            with self.assertRaisesRegex(ValueError, 'model failed'):
                with client:
                    raise ValueError('model failed')
        self.assertIsNotNone(self.gov.status()['active'])
        self.assertEqual(self.gov.acquire(request('other-worker', pid=101))['decision'], 'wait')
        self.assertEqual(self.gov.status()['stats']['completed'], 0)
        with patch('governor._owner_state', side_effect=lambda pid, created: 'dead' if pid == os.getpid() else 'alive'):
            self.assertEqual(self.gov.acquire(request('next-worker', pid=101))['decision'], 'cuda')

    def test_malformed_requests_are_rejected(self):
        for value in ({}, {**request(), 'owner_pid': True}, {**request(), 'owner_created': float('nan')},
                      {**request(), 'owner_created': 10**1000}, {**request(), 'stage': []},
                      {**request(), 'stage': 'arbitrary-command'}, {**request(), 'request_id': '../job'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.gov.acquire(value)
        with self.assertRaises(ValueError):
            self.gov.configure('true')


class OwnerIdentityTests(unittest.TestCase):
    def test_actual_pid_and_creation_time_and_reuse(self):
        created = psutil.Process().create_time()
        self.assertEqual(governor._owner_state(os.getpid(), created), 'alive')
        self.assertEqual(governor._owner_state(os.getpid(), created - 1), 'dead')


class ClientTests(unittest.TestCase):
    def grant(self, device='cpu', token='a' * 43):
        return {'decision': device, 'threads': 2, 'lease_id': token, 'retry_after_s': 0, 'reason': 'allowed'}

    def test_transport_failure_is_bounded_without_device_fallback(self):
        client = ResourceSession('whisper-asr', max_wait_s=.02, retry_interval_s=.005)
        with patch.object(client, '_post', side_effect=resource_client._TemporaryFailure('offline')):
            with self.assertRaisesRegex(RuntimeError, '작업을 시작하지 않습니다'):
                client.__enter__()
        self.assertIsNone(client.device)
        self.assertIsNone(client.lease_id)

    def test_context_releases_only_after_synchronous_work(self):
        client = ResourceSession('whisper-asr')
        responses = [self.grant(), self.grant(), {'decision': 'wait', 'released': True}]
        with patch.object(client, '_post', side_effect=responses) as post:
            with client as lease:
                self.assertEqual(lease.device, 'cpu')
                self.assertEqual(post.call_count, 1)
                lease.checkpoint()
            self.assertEqual(post.call_args.args[0], 'release')
            self.assertTrue(post.call_args.args[1]['completed'])

    def test_exception_keeps_lease_and_preserves_original_failure(self):
        client = ResourceSession('audio-preprocess')
        with patch.object(client, '_post', return_value=self.grant()) as post:
            with self.assertRaisesRegex(ValueError, 'stage failed') as raised:
                with client:
                    raise ValueError('stage failed')
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[0], 'acquire')
            self.assertIn('임대를 유지', raised.exception.__notes__[0])

    def test_checkpoint_never_changes_device_or_token(self):
        for second in (self.grant(device='cuda'), self.grant(token='b' * 43)):
            client = ResourceSession('whisper-asr')
            with patch.object(client, '_post', side_effect=[self.grant(), second]):
                client.__enter__()
                with self.assertRaisesRegex(RuntimeError, '변경할 수 없습니다'):
                    client.checkpoint()
                self.assertEqual(client.device, 'cpu')

    def test_invalid_or_unsupported_decision_does_not_execute(self):
        responses = [self.grant(device='cuda'), {'decision': 'cpu', 'threads': 9, 'lease_id': 'a' * 43},
                     {'decision': 'unknown'}, {'decision': 'cpu', 'threads': 1}]
        for response in responses:
            client = ResourceSession('audio-preprocess')
            with self.subTest(response=response), patch.object(client, '_post', return_value=response):
                with self.assertRaises(RuntimeError):
                    client.__enter__()
                self.assertIsNone(client.device)

    def test_403_refreshes_health_token_once(self):
        client = ResourceSession('audio-preprocess')
        health = lambda token: io.BytesIO(json.dumps({'app_id': 'resource-analyzer', 'csrf_token': token}).encode())
        failed = HTTPError(client.url, 403, 'Forbidden', {}, io.BytesIO(b''))
        responses = [health('a' * 43), failed, health('b' * 43), io.BytesIO(json.dumps(self.grant()).encode())]
        with patch.object(client._opener, 'open', side_effect=responses) as opened:
            result = client._post('acquire', request(stage='audio-preprocess'))
        self.assertEqual(result['decision'], 'cpu')
        self.assertEqual(opened.call_count, 4)
        self.assertEqual(opened.call_args_list[3].args[0].get_header('X-resource-analyzer-token'), 'b' * 43)

    def test_only_explicit_loopback_urls_are_accepted(self):
        for url in ('https://localhost:7442', 'http://example.com:7442', 'http://user@localhost:7442',
                    'http://127.0.0.1:7442/api', 'http://localhost', 'http://localhost:99999'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ResourceSession('whisper-asr', url=url)

    def test_response_size_and_wrong_health_are_rejected(self):
        client = ResourceSession('audio-preprocess')
        for data in (b'{}', b'[' + b' ' * (65 * 1024) + b']'):
            with self.subTest(data_length=len(data)), patch.object(client._opener, 'open', return_value=io.BytesIO(data)):
                with self.assertRaises(RuntimeError):
                    client._health()

    def test_redirects_cannot_forward_control_tokens(self):
        handler = resource_client._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, 'Found', {}, 'https://example.com'))


if __name__ == '__main__':
    unittest.main()
