"""Isolate native telemetry calls from HTTP and workload admission control."""
import ctypes
import json
import multiprocessing as mp
import os
import struct
import time

from temperature_metrics import TemperatureSampler

PAYLOAD_LIMIT = 8 * 1024 * 1024


def run_worker(buffer, requested, ready, stopping, factory=None):
    # The parent retains its sensor connection; restarting this process must not
    # repeatedly request administrator approval for the temperature helper.
    os.environ['RESOURCE_ANALYZER_DISABLE_SENSORS'] = '1'
    collector = None

    def publish(value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > PAYLOAD_LIMIT:
            payload = b'{"error":"Telemetry response exceeded its size limit"}'
        view = memoryview(buffer).cast('B')
        view[4:4 + len(payload)] = payload
        struct.pack_into('<I', view, 0, len(payload))
        ready.set()

    try:
        if factory is None:
            from collector import Collector
            factory = Collector
        collector = factory()
        publish({'ready': True})
        while not stopping.is_set():
            if not requested.wait(.2):
                continue
            requested.clear()
            if stopping.is_set():
                break
            try:
                publish({'sample': collector.sample()})
            except Exception as error:
                publish({'error': type(error).__name__ + ': ' + str(error)[:160]})
    except Exception as error:
        publish({'error': type(error).__name__ + ': ' + str(error)[:160]})
    finally:
        if collector is not None:
            collector.close()


class IsolatedCollector:
    def __init__(self, factory=None, startup_timeout=45, sample_timeout=15, sensors=True, cancel_event=None):
        context = mp.get_context('spawn')
        self.buffer = context.RawArray(ctypes.c_ubyte, PAYLOAD_LIMIT + 4)
        self.requested, self.ready, self.stopping = context.Event(), context.Event(), context.Event()
        self.timeout = sample_timeout
        self.cancel_event = cancel_event
        self.closed = False
        self.temperatures = TemperatureSampler(autostart=sensors)
        self.process = context.Process(target=run_worker, name='resource-sampling-worker',
            args=(self.buffer, self.requested, self.ready, self.stopping, factory), daemon=True)
        try:
            self.process.start()
            value = self.receive(startup_timeout)
            if value.get('ready') is not True:
                raise RuntimeError('Telemetry worker did not initialize')
        except BaseException:
            self.close()
            raise

    @property
    def pid(self):
        return self.process.pid

    def receive(self, timeout):
        # The producer publishes a complete bounded shared-memory message before
        # setting ready. A crash midway through a message cannot block a pipe read.
        deadline = time.monotonic() + timeout
        while not self.ready.wait(min(.2, max(0, deadline - time.monotonic()))):
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise InterruptedError('Telemetry collection was stopped')
            if self.process.exitcode is not None:
                raise RuntimeError(f'Telemetry worker exited ({self.process.exitcode})')
            if time.monotonic() >= deadline:
                raise TimeoutError('Telemetry worker stopped responding')
        size = struct.unpack_from('<I', self.buffer)[0]
        if not 0 < size <= PAYLOAD_LIMIT:
            raise RuntimeError('Invalid telemetry message length')
        value = json.loads(bytes(self.buffer[4:4 + size]))
        if not isinstance(value, dict):
            raise RuntimeError('Invalid telemetry message')
        if value.get('error'):
            raise RuntimeError(value['error'])
        return value

    def sample(self):
        if self.closed:
            raise RuntimeError('Telemetry worker is closed')
        self.ready.clear()
        self.requested.set()
        sample = self.receive(self.timeout).get('sample')
        if not isinstance(sample, dict):
            raise RuntimeError('Telemetry worker returned no sample')
        sample['system']['temperatures'] = self.temperatures.sample()
        return sample

    def close(self):
        if self.closed:
            return
        self.stopping.set()
        self.requested.set()
        try:
            if self.process.pid is not None:
                self.process.join(timeout=1)
                if self.process.is_alive():
                    # Only the dedicated sampler owned by this instance. It never
                    # owns a governor lease or runs user workloads.
                    self.process.terminate()
                    self.process.join(timeout=3)
                if self.process.is_alive():
                    raise RuntimeError('Telemetry worker shutdown is still pending')
            self.closed = True
        finally:
            self.temperatures.close()
