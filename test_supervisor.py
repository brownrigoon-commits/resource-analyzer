import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from runtime_state import read_intent, write_intent
from supervisor import watch_server


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(port=17449, data_dir=str(self.root), autostart=True, stop=False)
        self.addCleanup(patch.stopall)
        patch('entrypoint.port_busy', return_value=False).start()
        patch('supervisor.LOG').start()
        self.wait = patch('supervisor.wait_for_stop', return_value=False).start()
        self.spawn = patch('entrypoint.background_process').start()

    def test_exited_server_is_restarted_without_losing_collection_intent(self):
        first, second = Mock(pid=1), Mock(pid=2)
        def crashed():
            write_intent(self.root / 'collection-intent.json', self.args.supervision_session, True)
            return 17
        def stopped():
            (self.root / 'STOP').write_text('explicit stop')
            return 0
        first.wait.side_effect, second.wait.side_effect = crashed, stopped
        self.spawn.side_effect = [first, second]
        self.assertEqual(watch_server(self.args, self.root), 0)
        self.assertEqual(self.spawn.call_count, 2)
        self.assertIn('--autostart', self.spawn.call_args_list[1].args[0])
        first.terminate.assert_not_called()
        second.terminate.assert_not_called()

    def test_user_paused_collection_does_not_resume_after_server_failure(self):
        first, second = Mock(pid=1), Mock(pid=2)
        def crashed():
            write_intent(self.root / 'collection-intent.json', self.args.supervision_session, False)
            return 1
        def stopped():
            (self.root / 'STOP').write_text('explicit stop')
            return 0
        first.wait.side_effect, second.wait.side_effect = crashed, stopped
        self.spawn.side_effect = [first, second]
        watch_server(self.args, self.root)
        self.assertNotIn('--autostart', self.spawn.call_args_list[1].args[0])

    def test_explicit_service_stop_never_restarts(self):
        child = Mock(pid=1)
        def stopped():
            (self.root / 'STOP').write_text('explicit stop')
            return 0
        child.wait.side_effect = stopped
        self.spawn.return_value = child
        watch_server(self.args, self.root)
        self.assertEqual(self.spawn.call_count, 1)
        self.wait.assert_not_called()

    def test_wrong_session_or_corrupt_intent_is_not_reused(self):
        path = self.root / 'collection-intent.json'
        write_intent(path, 'old-session', True)
        self.assertFalse(read_intent(path, 'new-session', False))
        path.write_text('{broken')
        self.assertFalse(read_intent(path, 'new-session', False))

    def test_stop_requested_at_retry_boundary_prevents_another_start(self):
        child = Mock(pid=1)
        child.wait.return_value = 17
        self.spawn.return_value = child
        def stopping(path, delay):
            path.write_text('explicit stop at restart boundary')
            return False
        self.wait.side_effect = stopping
        watch_server(self.args, self.root)
        self.assertEqual(self.spawn.call_count, 1)


if __name__ == '__main__':
    unittest.main()
