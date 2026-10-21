"""Real loopback reads retain identity and fail-closed update guards."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

import entrypoint


class Handler(BaseHTTPRequestHandler):
    status = 200
    payload = b'{"app_id":"resource-analyzer","version":"synthetic"}'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(self.status)
        self.send_header('Content-Length', str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


class LauncherHTTPTests(unittest.TestCase):
    def setUp(self):
        Handler.status = 200
        Handler.payload = b'{"app_id":"resource-analyzer","version":"synthetic"}'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def test_local_health_works_when_unrelated_tls_setup_fails(self):
        with patch('ssl._create_default_https_context', side_effect=FileNotFoundError('synthetic TLS file missing')) as tls:
            self.assertEqual(entrypoint.health(self.port)['app_id'], 'resource-analyzer')
            tls.assert_not_called()

    def test_wrong_application_is_never_accepted(self):
        Handler.payload = b'{"app_id":"another-app"}'
        self.assertIsNone(entrypoint.health(self.port))

    def test_redirect_and_non_object_payload_are_not_accepted(self):
        Handler.status = 302
        self.assertIsNone(entrypoint.health(self.port))
        Handler.status = 200
        Handler.payload = b'[]'
        self.assertIsNone(entrypoint.health(self.port))

    def test_oversized_control_response_is_rejected(self):
        Handler.payload = json.dumps({'value': 'x' * 65537}).encode()
        self.assertIsNone(entrypoint.health(self.port))


if __name__ == '__main__':
    unittest.main()
