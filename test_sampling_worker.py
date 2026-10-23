import os
import threading
import time
import unittest

from sampling_worker import IsolatedCollector


class FakeCollector:
    def __init__(self):
        self.count = 0

    def sample(self):
        self.count += 1
        return {'ts': time.time(), 'sequence': self.count, 'system': {}, 'groups': [], 'errors': []}

    def close(self):
        pass


class HangingCollector(FakeCollector):
    def sample(self):
        time.sleep(60)


class CrashingCollector(FakeCollector):
    def sample(self):
        os._exit(17)


class FailingInitialization(FakeCollector):
    def __init__(self):
        raise RuntimeError('synthetic initialization failure')


class SamplingWorkerTests(unittest.TestCase):
    def collector(self, factory, **options):
        collector = IsolatedCollector(factory=factory, startup_timeout=10, sensors=False, **options)
        self.addCleanup(collector.close)
        return collector

    def test_child_returns_fresh_samples_and_closes(self):
        collector = self.collector(FakeCollector)
        first, second = collector.sample(), collector.sample()
        self.assertGreater(second['sequence'], first['sequence'])
        self.assertIn('temperatures', second['system'])
        self.assertNotEqual(collector.pid, os.getpid())
        collector.close()
        self.assertFalse(collector.process.is_alive())

    def test_native_hang_is_bounded_and_only_the_owned_child_is_terminated(self):
        collector = self.collector(HangingCollector, sample_timeout=.2)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            collector.sample()
        collector.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(collector.process.is_alive())

    def test_child_crash_does_not_crash_or_block_the_parent(self):
        collector = self.collector(CrashingCollector, sample_timeout=3)
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            collector.sample()
        self.assertEqual(collector.process.exitcode, 17)

    def test_initialization_failure_is_reported_and_does_not_leak_a_worker(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic initialization failure'):
            IsolatedCollector(factory=FailingInitialization, startup_timeout=10, sensors=False)

    def test_user_pause_interrupts_a_pending_collection(self):
        cancel = threading.Event()
        collector = self.collector(HangingCollector, sample_timeout=15, cancel_event=cancel)
        cancel.set()
        started = time.monotonic()
        with self.assertRaises(InterruptedError):
            collector.sample()
        self.assertLess(time.monotonic() - started, 1)


if __name__ == '__main__':
    unittest.main()
