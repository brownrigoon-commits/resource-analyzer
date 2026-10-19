"""Local opt-in and request-boundary tests; no real collection in these tests."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from server import Monitor, make_handler, voice_status


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.monitor = Monitor(Path(self.temp.name))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        self.port = self.server.server_port
        self.server.RequestHandlerClass = make_handler(self.monitor, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.port}'

    def tearDown(self):
        self.monitor.close()
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, token=None, origin=None):
        headers = {}
        if token is not None:
            headers['X-Resource-Analyzer-Token'] = token
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(self.base+path, data=b'{}' if token is not None else None, headers=headers)
        return urllib.request.urlopen(req, timeout=5)

    def test_health_does_not_start_collection(self):
        with patch('server.Collector') as collector:
            health = json.load(self.request('/api/health'))
            self.assertEqual(health['app_id'], 'resource-analyzer')
            self.assertFalse(health['collecting'])
            collector.assert_not_called()
            self.assertIsNone(self.monitor.thread)

    def test_speedtest_assets_are_local_and_connections_are_endpoint_scoped(self):
        for path in ('/speedtest.mjs', '/vendor/cloudflare-speedtest-1.14.2.mjs'):
            with self.request(path) as response:
                self.assertIn('javascript', response.headers['Content-Type'])
                self.assertGreater(len(response.read()), 100)
                policy = response.headers['Content-Security-Policy']
                self.assertIn('https://speed.cloudflare.com/__down', policy)
                self.assertIn('https://speed.cloudflare.com/__up', policy)
                self.assertNotIn('__results', policy)
                self.assertIn("script-src 'self'", policy)
        self.assertFalse(self.monitor.collecting)

    def test_start_is_authorized_and_idempotent(self):
        with patch.object(Monitor, 'run', lambda monitor: monitor.recording_stop.wait()):
            token = self.monitor.csrf_token
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self.request('/api/monitor/start', 'invalid')
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self.request('/api/monitor/start', token, 'https://unrelated.example')
            self.assertEqual(denied.exception.code, 403)
            self.assertTrue(json.load(self.request('/api/monitor/start', token))['collecting'])
            first_thread = self.monitor.thread
            self.request('/api/monitor/start', token).close()
            self.assertIs(self.monitor.thread, first_thread)
            self.assertFalse(json.load(self.request('/api/monitor/stop', token))['collecting'])

    def test_private_integration_is_optional(self):
        self.assertEqual(voice_status(Path(self.temp.name)), {'available': False})


if __name__ == '__main__':
    unittest.main()
