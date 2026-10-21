"""Update guards use mocks: no live application is stopped or started."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / 'ResourceAnalyzer.exe'
        self.exe.write_bytes(b'synthetic executable')
        (self.root / 'BUILD-INFO.txt').write_text(json.dumps({
            'app_version': updater.VERSION,
            'executable_sha256': hashlib.sha256(self.exe.read_bytes()).hexdigest(),
        }), encoding='utf-8')
        self.args = argparse.Namespace(port=7442, stop=False, autostart=True, no_browser=True)
        self.addCleanup(patch.stopall)
        patch('sys.frozen', True, create=True).start()
        patch('sys.executable', str(self.exe)).start()
        self.health = patch('entrypoint.health', side_effect=[
            {'version': '0.5.0', 'collecting': False}, {'version': updater.VERSION},
        ]).start()
        self.busy = patch('entrypoint.port_busy', return_value=False).start()
        self.serve = patch('entrypoint.serve', return_value=0).start()
        self.launch = patch('entrypoint.launch', return_value=0).start()
        self.open = patch('entrypoint.local_json').start()
        self.governor(active=None)

    def governor(self, **data):
        self.open.return_value = data

    def test_verified_update_uses_normal_stop_and_preserves_collection_state(self):
        self.assertEqual(updater.apply_update(self.args, self.root), 0)
        self.serve.assert_called_once()
        self.launch.assert_called_once()
        self.assertFalse(self.args.stop)
        self.assertFalse(self.args.autostart)

    def test_tampered_package_never_touches_running_app(self):
        self.exe.write_bytes(b'tampered')
        with self.assertRaisesRegex(RuntimeError, 'verification'):
            updater.apply_update(self.args, self.root)
        self.health.assert_not_called()
        self.serve.assert_not_called()
        self.launch.assert_not_called()

    def test_active_worker_is_never_stopped(self):
        self.governor(active={'stage': 'qwen-vision'})
        with self.assertRaisesRegex(RuntimeError, 'worker is active'):
            updater.apply_update(self.args, self.root)
        self.serve.assert_not_called()
        self.launch.assert_not_called()

    def test_unidentified_port_is_never_stopped(self):
        self.health.side_effect = [None]
        self.busy.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'unidentified'):
            updater.apply_update(self.args, self.root)
        self.serve.assert_not_called()
        self.launch.assert_not_called()

    def test_already_updated_does_not_restart(self):
        self.health.side_effect = [{'version': updater.VERSION}]
        self.governor(active=None, adapters=[{'stage': 'qwen-vision'}])
        updater.apply_update(self.args, self.root)
        self.serve.assert_not_called()
        self.launch.assert_called_once()

    def test_failed_normal_stop_never_launches(self):
        self.serve.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'shutdown failed'):
            updater.apply_update(self.args, self.root)
        self.assertFalse(self.args.stop)
        self.launch.assert_not_called()

    def test_pending_normal_stop_never_forces_shutdown_or_launches(self):
        self.busy.return_value = True
        with patch('updater.time.monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(RuntimeError, 'still pending'):
                updater.apply_update(self.args, self.root)
        self.serve.assert_called_once()
        self.launch.assert_not_called()

    def test_wrong_response_version_is_not_reported_as_success(self):
        self.health.side_effect = [{'version': '0.5.0'}, {'version': '0.5.0'}]
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            updater.apply_update(self.args, self.root)


if __name__ == '__main__':
    unittest.main()
