"""Exercise real loopback request boundaries with synthetic resource readings."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

import psutil

from server import Monitor, make_handler


class GovernorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.monitor = Monitor(Path(self.temp.name))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        self.port = self.server.server_port
        self.server.RequestHandlerClass = make_handler(self.monitor, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.monitor.close()
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, body=None, token=None, origin=None, host=None):
        headers = {}
        if token is not None:
            headers['X-Resource-Analyzer-Token'] = token
        if origin:
            headers['Origin'] = origin
        if host:
            headers['Host'] = host
        data = None if body is None else json.dumps(body).encode()
        with self.opener.open(urllib.request.Request(self.base+path, data=data, headers=headers), timeout=5) as response:
            return json.load(response)

    def feed(self, gpu=95):
        self.monitor.governor.update({'collection_duration_s': .02, 'errors': [],
            'system': {'cpu_pct': 10, 'ram_pct': 40, 'ram_available_gb': 20,
                'gpus': [{'util_pct': gpu, 'source': 'NVML'}]}}, observed_at=time.monotonic())

    def test_control_requires_token_and_local_origin(self):
        for token, origin, host in [('invalid', None, None),
                                    (self.monitor.csrf_token, 'https://external.example', None),
                                    (self.monitor.csrf_token, None, 'external.example')]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/governor/configure', {'enabled': True}, token, origin, host)
            self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.monitor.governor.status()['enabled'])

    def test_configure_is_explicit_and_starts_collection(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/governor/configure', {'enabled': 'yes'}, self.monitor.csrf_token)
        self.assertEqual(error.exception.code, 400)
        with patch.object(self.monitor, 'start') as start:
            value = self.request('/api/governor/configure', {'enabled': True}, self.monitor.csrf_token)
        self.assertTrue(value['enabled'])
        start.assert_called_once()

    def test_unknown_stage_and_wrong_owner_are_rejected(self):
        payload = {'stage': 'arbitrary-code', 'owner_pid': psutil.Process().pid,
                   'owner_created': psutil.Process().create_time(), 'request_id': 'http-test'}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/governor/acquire', payload, self.monitor.csrf_token)
        self.assertEqual(error.exception.code, 400)
        payload.update(stage='whisper-asr', owner_created=1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/governor/acquire', payload, self.monitor.csrf_token)
        self.assertEqual(error.exception.code, 403)

    def test_sdk_uses_cpu_under_pressure_and_returns_actual_completion(self):
        from resource_client import ResourceSession
        self.monitor.governor.configure(True)
        self.feed()
        with ResourceSession('whisper-asr', url=self.base) as worker:
            self.assertEqual(worker.device, 'cpu')
            self.assertLessEqual(worker.threads, 8)
            worker.checkpoint()
            status = self.request('/api/governor')
            self.assertEqual(status['active']['device'], 'cpu')
        status = self.request('/api/governor')
        self.assertIsNone(status['active'])
        self.assertEqual(status['stats']['cpu_completed'], 1)
        self.assertNotIn('lease_id', json.dumps(status))


if __name__ == '__main__':
    unittest.main()
