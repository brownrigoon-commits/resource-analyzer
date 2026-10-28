from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from runtime_state import read_intent
from server import Monitor, history_frame, make_handler
from sampling_worker import WorkerCleanupPending


def frame():
    return {'ts': time.time(), 'errors': [], 'system': {'cpu_pct': 10, 'cpu_peak_core_pct': 20,
        'ram_pct': 40, 'ram_available_gb': 20, 'disk_read_mbps': 1, 'disk_write_mbps': 1,
        'gpus': [], 'disks': []}, 'groups': []}


class Collector:
    pid = 123

    def __init__(self, callback=None):
        self.closed = False
        self.callback = callback

    def sample(self):
        if self.callback:
            self.callback()
        return frame()

    def close(self):
        self.closed = True


class CollectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.monitor = Monitor(Path(self.temp.name))
        self.addCleanup(self.monitor.close)
        patch('server.SAMPLE_INTERVAL_S', .01).start()
        patch('server.SUMMARY_INTERVAL_S', .03).start()
        patch('server.LOG').start()
        patch('server.TemperatureSampler').start()

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Recovery did not complete')
            time.sleep(.01)

    def test_initialization_failure_retries_without_browser_or_manual_start(self):
        worker = Collector()
        with patch('server.Collector', side_effect=[RuntimeError('initialization failed'), worker]):
            self.monitor.start()
            self.wait(lambda: self.monitor.latest is not None)
            self.assertEqual(self.monitor.recovery_attempts, 1)
            self.assertTrue(self.monitor.collecting)
            self.assertIsNone(self.monitor.error)
            self.monitor.pause()
        self.assertTrue(worker.closed)

    def test_sample_failure_replaces_sampler_and_keeps_http_monitor_alive(self):
        failing, healthy = Collector(), Collector()
        failing.sample = Mock(side_effect=TimeoutError('synthetic driver stall'))
        with patch('server.Collector', side_effect=[failing, healthy]):
            self.monitor.start()
            self.wait(lambda: self.monitor.latest is not None)
            self.assertTrue(failing.closed)
            self.assertEqual(self.monitor.recovery_attempts, 1)
            self.assertFalse(self.monitor.snapshot()[0]['stale'])
            self.monitor.pause()

    def test_storage_failure_keeps_live_sampling_and_bounds_pending_memory(self):
        worker = Collector()
        calls = []

        def count():
            calls.append(1)
            if len(calls) >= 85:
                self.monitor.recording_stop.set()
        worker.callback = count
        with patch('server.Collector', return_value=worker), patch.object(self.monitor.history, 'record', side_effect=OSError('storage offline')) as record:
            self.monitor.start()
            self.wait(lambda: not self.monitor.collecting)
            self.assertGreaterEqual(len(calls), 85)
            self.assertGreater(self.monitor.dropped_history_samples, 0)
            self.assertTrue(all(len(call.args[0]) <= 60 for call in record.call_args_list))
            self.assertIn('storage offline', self.monitor.storage_error)
            self.assertTrue(worker.closed)

    def test_delayed_exit_retries_cleanup_before_creating_replacement(self):
        failing, healthy = Collector(), Collector()
        failing.sample = Mock(side_effect=TimeoutError('driver stalled'))
        attempts = []
        def close():
            attempts.append(1)
            if len(attempts) < 3:
                raise WorkerCleanupPending(failing)
            failing.closed = True
        failing.close = close
        def create(**kwargs):
            if not attempts:
                return failing
            self.assertTrue(failing.closed, 'No overlapping native samplers')
            return healthy
        with patch('server.Collector', side_effect=create) as factory:
            self.monitor.start()
            self.wait(lambda: self.monitor.latest is not None, timeout=12)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(len(attempts), 3)
            self.assertTrue(self.monitor.collecting)
            self.assertIsNone(self.monitor.error)
            self.assertIsNone(self.monitor.retiring_collector)
            self.monitor.pause()

    def test_initialization_cleanup_pending_keeps_the_owned_sampler(self):
        pending, healthy = Collector(), Collector()
        with patch('server.Collector', side_effect=[WorkerCleanupPending(pending), healthy]) as factory:
            self.monitor.start()
            self.wait(lambda: self.monitor.latest is not None)
            self.assertTrue(pending.closed)
            self.assertEqual(factory.call_count, 2)
            self.assertTrue(self.monitor.collecting)
            self.monitor.pause()

    def test_pending_cleanup_is_retained_across_explicit_pause_and_start(self):
        pending, healthy = Collector(), Collector()
        pending.callback = lambda: self.monitor.recording_stop.set()
        pending.close = Mock(side_effect=[WorkerCleanupPending(pending), None])
        with patch('server.Collector', side_effect=[pending, healthy]) as factory:
            self.monitor.start()
            self.wait(lambda: not self.monitor.collecting)
            self.assertIs(self.monitor.retiring_collector, pending)
            self.monitor.start()
            self.wait(lambda: factory.call_count == 2 and self.monitor.error is None)
            self.assertEqual(pending.close.call_count, 2)
            self.assertIsNone(self.monitor.retiring_collector)
            self.monitor.pause()

    def test_explicit_pause_is_persisted_but_internal_close_preserves_intent(self):
        with patch('server.Collector', return_value=Collector()):
            self.monitor.start()
            self.monitor.close()
        self.assertTrue(read_intent(self.monitor.intent_path, self.monitor.intent_session, False))
        self.monitor.pause()
        self.assertFalse(read_intent(self.monitor.intent_path, self.monitor.intent_session, True))

    def test_history_buffer_excludes_process_lists_and_task_details(self):
        value = frame()
        value['sample_duration_s'] = 1
        value['groups'] = [{'id': 'example', 'name': 'Example', 'cpu_pct': 10, 'ram_gb': 1,
            'gpu_pct': None, 'io_read_mbps': 0, 'io_write_mbps': 0,
            'processes': [{'pid': 42, 'task_context': {'title': 'Private task'}}]}]
        self.assertNotIn('processes', history_frame(value)['groups'][0])

    def test_browser_disconnect_does_not_raise_a_service_error(self):
        handler = make_handler(self.monitor, 7442).__new__(make_handler(self.monitor, 7442))
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            handler.wfile.write.side_effect = error()
            handler.send(b'sample', 'application/json')


if __name__ == '__main__':
    unittest.main()
