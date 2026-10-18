from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import psutil

from governor import Governor
from resource_client import FileResourceSession, _TemporaryFailure
import resource_ipc as ipc


def sample(gpu=60):
    return {'collection_duration_s': .1, 'errors': [], 'system': {
        'cpu_pct': 10, 'ram_pct': 30, 'ram_available_gb': 20,
        'gpus': [{'id': 'gpu-0', 'source': 'NVML', 'util_pct': gpu}]}}


def owner_request(ident=None):
    return {'stage': 'whisper-asr', 'owner_pid': os.getpid(),
            'owner_created': psutil.Process().create_time(), 'request_id': ident or uuid.uuid4().hex}


class FileIPCTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gov = Governor(self.directory.name)
        self.addCleanup(self.gov.close)
        self.gov.configure(True)
        self.gov.update(sample(), observed_at=time.monotonic())
        self.token = secrets.token_urlsafe(32)
        self.server = ipc.LocalIPC(self.gov, self.directory.name, self.token, 'test-1').start()
        self.addCleanup(self.server.close)
        self.folder = Path(self.directory.name) / 'ipc'

    def client(self, stage='whisper-asr'):
        return FileResourceSession(stage, self.directory.name, max_wait_s=2, retry_interval_s=.01)

    def wait_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('IPC condition did not complete within %ss' % timeout)

    def request_file(self, operation, payload, *, signature=True):
        ident = uuid.uuid4().hex
        body = {'protocol': ipc.PROTOCOL, 'id': ident, 'nonce': secrets.token_hex(32),
                'operation': operation, 'payload': payload}
        value = ipc.envelope(self.token, body)
        if not signature:
            value['signature'] = '0' * 64
        path = self.folder / (ident + '.requests.json')
        ipc._atomic_json(path, value, ipc.REQUEST_LIMIT)
        return ident, path

    def test_real_file_session_works_with_all_socket_connection_functions_blocked(self):
        denied = AssertionError('offline guard: all socket connections disabled')
        with patch.object(socket.socket, 'connect', side_effect=denied), \
                patch.object(socket.socket, 'connect_ex', side_effect=denied), \
                patch.object(socket, 'create_connection', side_effect=denied), \
                patch.object(socket, 'getaddrinfo', side_effect=denied):
            with self.client() as lease:
                self.assertEqual(lease.device, 'cpu')
                self.assertLessEqual(lease.threads, 8)
                lease.checkpoint()
                self.assertIsNotNone(self.gov.status()['active'])
        self.assertIsNone(self.gov.status()['active'])
        self.assertEqual(self.gov.status()['stats']['cpu_completed'], 1)
        self.assertEqual(list(self.folder.glob('*.requests.json')), [])
        self.assertEqual(list(self.folder.glob('*.response.json')), [])

    def test_request_hmac_and_fixed_operation_whitelist(self):
        ident, path = self.request_file('acquire', owner_request(), signature=False)
        self.wait_until(lambda: not path.exists())
        self.assertFalse((self.folder / (ident + '.response.json')).exists())
        self.assertIsNone(self.gov.status()['active'])
        ident, path = self.request_file('__getattribute__', {'command': 'never execute'})
        response = self.folder / (ident + '.response.json')
        self.wait_until(response.exists)
        value = ipc._read_json(response, ipc.RESPONSE_LIMIT)
        self.assertTrue(ipc.authenticated(self.token, value))
        self.assertEqual(value['error'], 'ValueError')
        self.assertIsNone(self.gov.status()['active'])
        response.unlink()

    def test_malformed_and_oversized_requests_never_reach_governor(self):
        for data in (b'[]', b'{invalid', b'{' + b' ' * ipc.REQUEST_LIMIT + b'}'):
            path = self.folder / (uuid.uuid4().hex + '.requests.json')
            path.write_bytes(data)
            self.wait_until(lambda: not path.exists())
        self.assertIsNone(self.gov.status()['active'])

    def test_only_fixed_uuid_request_names_are_consumed(self):
        for name in ('arbitrary.json', 'not-a-uuid.requests.json', 'a' * 31 + '.requests.json', 'keep.wav'):
            (self.folder / name).write_text('preserve', encoding='utf-8')
        time.sleep(.15)
        for name in ('arbitrary.json', 'not-a-uuid.requests.json', 'a' * 31 + '.requests.json', 'keep.wav'):
            self.assertEqual((self.folder / name).read_text(encoding='utf-8'), 'preserve')

    def test_metadata_has_owner_identity_and_another_server_cannot_replace_it(self):
        metadata = ipc.read_metadata(self.folder)
        self.assertEqual(metadata['app_id'], 'resource-analyzer')
        self.assertTrue(ipc.server_alive(metadata))
        other = ipc.LocalIPC(self.gov, self.directory.name, 'x' * 43, 'test-2')
        with self.assertRaises(PermissionError):
            other.start()
        self.assertEqual(ipc.read_metadata(self.folder)['token'], self.token)
        other.close()
        self.assertEqual(ipc.read_metadata(self.folder)['token'], self.token)

    def test_server_restart_token_is_used_for_an_existing_lease(self):
        client = self.client()
        client.__enter__()
        original_lease = client.lease_id
        self.server.close()
        self.assertFalse((self.folder / 'server.json').exists())
        self.server = ipc.LocalIPC(self.gov, self.directory.name, 'b' * 43, 'test-2').start()
        self.addCleanup(self.server.close)
        client.checkpoint()
        self.assertEqual(client.lease_id, original_lease)
        client.__exit__(None, None, None)
        self.assertIsNone(self.gov.status()['active'])

    def test_close_waits_for_current_response_before_removing_metadata(self):
        client = self.client()
        started, finish = threading.Event(), threading.Event()
        original = self.gov.acquire
        def blocked(payload):
            started.set()
            if not finish.wait(2):
                raise RuntimeError('test release timed out')
            return original(payload)
        with patch.object(self.gov, 'acquire', side_effect=blocked), ThreadPoolExecutor(max_workers=2) as workers:
            future = workers.submit(client.__enter__)
            self.assertTrue(started.wait(1))
            close = workers.submit(self.server.close)
            time.sleep(.05)
            self.assertFalse(close.done())
            self.assertTrue((self.folder / 'server.json').exists())
            finish.set()
            self.assertEqual(future.result(timeout=2).device, 'cpu')
            close.result(timeout=2)
        self.assertFalse((self.folder / 'server.json').exists())
        # The worker has not completed; stopping IPC never clears its lease.
        self.assertIsNotNone(self.gov.status()['active'])

    def test_close_never_removes_another_servers_metadata(self):
        current = ipc.read_metadata(self.folder)
        current['token'] = 'z' * 43
        ipc._atomic_json(self.folder / 'server.json', current, ipc.METADATA_LIMIT, replace=True)
        self.server.close()
        self.assertEqual(ipc.read_metadata(self.folder)['token'], 'z' * 43)

    def test_failure_keeps_the_living_workers_lease(self):
        with self.assertRaisesRegex(ValueError, 'model failed'):
            with self.client():
                raise ValueError('model failed')
        self.assertIsNotNone(self.gov.status()['active'])
        other = self.client()
        response = other._post('acquire', owner_request())
        self.assertEqual(response['decision'], 'wait')
        self.assertEqual(self.gov.status()['stats']['completed'], 0)

    def test_request_publication_is_exclusive(self):
        path = self.folder / (uuid.uuid4().hex + '.response.json')
        ipc._atomic_json(path, {'preserve': True}, 4096)
        with self.assertRaises(FileExistsError):
            ipc._atomic_json(path, {'replace': True}, 4096)
        self.assertEqual(ipc._read_json(path, 4096), {'preserve': True})
        self.assertFalse(list(self.folder.glob('.*.tmp')))

    def test_concurrent_file_requests_preserve_one_heavy_lease(self):
        def acquire(index):
            payload = owner_request('file-job-%s' % index)
            client = FileResourceSession('whisper-asr', self.directory.name, max_wait_s=10)
            return payload, client._post('acquire', payload)
        with ThreadPoolExecutor(max_workers=20) as workers:
            results = list(workers.map(acquire, range(40)))
        grants = [(payload, result) for payload, result in results if result['decision'] != 'wait']
        self.assertEqual(len(grants), 1)
        payload, grant = grants[0]
        response = self.client()._post('release', {**payload, 'lease_id': grant['lease_id'], 'completed': True})
        self.assertTrue(response['released'])
        self.assertEqual(self.gov.status()['stats']['deferred'], 39)

    def test_symlink_files_and_directory_are_rejected(self):
        target = Path(self.directory.name) / 'preserve.json'
        target.write_text('{"preserve":true}', encoding='utf-8')
        link = self.folder / (uuid.uuid4().hex + '.requests.json')
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest('Windows symlink privilege unavailable: ' + str(error))
        with self.assertRaises(ValueError):
            ipc._read_json(link, 4096)
        time.sleep(.15)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding='utf-8'), '{"preserve":true}')
        alternate = Path(self.directory.name) / 'alias'
        alternate.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(ValueError):
            ipc.mailbox(alternate)
        alternate.unlink()
        link.unlink()


class FileIPCValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = ipc.mailbox(self.directory.name, create=True)
        self.token = 'a' * 43
        self.metadata = {'app_id': ipc.APP_ID, 'protocol': ipc.PROTOCOL, 'version': 'test',
                         'token': self.token, 'server_pid': os.getpid(),
                         'server_created': psutil.Process().create_time()}
        ipc._atomic_json(self.folder / 'server.json', self.metadata, ipc.METADATA_LIMIT)

    def client(self, **kwargs):
        return FileResourceSession('whisper-asr', self.directory.name, max_wait_s=.1,
                                   retry_interval_s=.005, **kwargs)

    def fake_response(self, mutation):
        original = ipc._atomic_json
        def write(path, value, limit, **kwargs):
            original(path, value, limit, **kwargs)
            if str(path).endswith('.requests.json'):
                body = {'protocol': ipc.PROTOCOL, 'id': value['id'], 'nonce': value['nonce'], 'ok': True,
                        'result': {'decision': 'cpu', 'threads': 2, 'lease_id': 'x' * 43}}
                response = ipc.envelope(self.token, body)
                mutation(response)
                original(self.folder / (value['id'] + '.response.json'), response, ipc.RESPONSE_LIMIT)
        return write

    def test_bad_response_nonce_and_hmac_fail_closed(self):
        for mutation in (lambda response: response.update(nonce='0' * 64),
                         lambda response: response.update(signature='0' * 64)):
            client = self.client()
            with self.subTest(mutation=mutation), patch('resource_ipc._atomic_json', side_effect=self.fake_response(mutation)):
                with self.assertRaisesRegex(RuntimeError, '서명 또는 요청 nonce'):
                    client.__enter__()
            self.assertIsNone(client.device)
            self.assertEqual(list(self.folder.glob('*.requests.json')), [])
            self.assertEqual(list(self.folder.glob('*.response.json')), [])

    def test_wrong_metadata_and_reused_pid_fail_closed(self):
        for change in ({'app_id': 'another-app'}, {'server_created': self.metadata['server_created'] - 10},
                       {'server_created': 10**1000}):
            ipc._atomic_json(self.folder / 'server.json', {**self.metadata, **change}, ipc.METADATA_LIMIT, replace=True)
            client = self.client()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                client.__enter__()
            self.assertIsNone(client.device)

    def test_no_server_and_oversized_response_never_fall_back(self):
        client = self.client()
        with self.assertRaisesRegex(RuntimeError, '작업을 시작하지 않습니다'):
            client.__enter__()
        self.assertIsNone(client.device)
        def oversized(response):
            response['padding'] = 'x' * ipc.RESPONSE_LIMIT
        # Use raw bytes to emulate an untrusted response exceeding the limit.
        original = ipc._atomic_json
        def write(path, value, limit, **kwargs):
            original(path, value, limit, **kwargs)
            if str(path).endswith('.requests.json'):
                (self.folder / (value['id'] + '.response.json')).write_bytes(b'{' + b' ' * ipc.RESPONSE_LIMIT + b'}')
        client = self.client()
        with patch('resource_ipc._atomic_json', side_effect=write), self.assertRaises(RuntimeError):
            client.__enter__()
        self.assertIsNone(client.device)

    def test_metadata_changed_during_request_retries_without_old_token(self):
        original = ipc._atomic_json
        def restart(path, value, limit, **kwargs):
            original(path, value, limit, **kwargs)
            if str(path).endswith('.requests.json'):
                original(self.folder / 'server.json', {**self.metadata, 'token': 'b' * 43}, ipc.METADATA_LIMIT, replace=True)
        client = self.client()
        with patch('resource_ipc._atomic_json', side_effect=restart), self.assertRaises(_TemporaryFailure):
            client._post('acquire', owner_request())
        self.assertEqual(list(self.folder.glob('*.requests.json')), [])

    def test_reader_rejects_duplicate_json_fields_and_nonfinite_numbers(self):
        path = self.folder / 'reader-test.json'
        for data in (b'{"key":1,"key":2}', b'{"number":NaN}', b'{"number":Infinity}'):
            path.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                ipc._read_json(path, 4096)

    def test_windows_reparse_metadata_and_parent_folder_fail_closed_without_symlink_privilege(self):
        original = Path.lstat
        for target in (self.folder, self.folder / 'server.json'):
            def reparse(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size,
                                           st_dev=info.st_dev, st_ino=info.st_ino,
                                           st_file_attributes=getattr(info, 'st_file_attributes', 0) | 0x400)
                return info
            client = self.client()
            with self.subTest(target=target), patch.object(Path, 'lstat', autospec=True, side_effect=reparse):
                with self.assertRaisesRegex(RuntimeError, '메타데이터 검증'):
                    client.__enter__()
            self.assertIsNone(client.device)
            self.assertEqual(list(self.folder.glob('*.requests.json')), [])
            self.assertEqual(ipc.read_metadata(self.folder)['token'], self.token)


if __name__ == '__main__':
    unittest.main()
