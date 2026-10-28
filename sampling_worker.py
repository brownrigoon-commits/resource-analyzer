"""Isolate native telemetry calls from HTTP and workload admission control."""
import ctypes
import json
import multiprocessing as mp
import os
import struct
import threading
import time

from temperature_metrics import TemperatureSampler

PAYLOAD_LIMIT = 8 * 1024 * 1024
PHASES = ('starting', 'initializing', 'system', 'gpu', 'process-counters', 'process-details', 'process-fallback',
          'task-context', 'disk-capacity', 'inventory', 'publishing', 'waiting')


class WorkerCleanupPending(RuntimeError):
    """Retain ownership when Windows has not finished terminating a sampler."""
    def __init__(self, collector):
        self.collector = collector
        super().__init__('Telemetry worker shutdown is still pending')


def run_worker(buffer, requested, ready, stopping, phase, factory=None):
    # The parent retains its sensor connection; restarting this process must not
    # repeatedly request administrator approval for the temperature helper.
    os.environ['RESOURCE_ANALYZER_DISABLE_SENSORS'] = '1'
    collector = None
    parent = mp.parent_process()
    if parent is not None:
        def follow_parent():
            while not stopping.value:
                if not parent.is_alive():
                    os._exit(0)
                time.sleep(.2)
        threading.Thread(target=follow_parent, name='sampling-parent-watch', daemon=True).start()

    def publish(value, sequence):
        phase.value = PHASES.index('publishing')
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > PAYLOAD_LIMIT:
            payload = b'{"error":"Telemetry response exceeded its size limit"}'
        view = memoryview(buffer).cast('B')
        view[4:4 + len(payload)] = payload
        struct.pack_into('<I', view, 0, len(payload))
        ready.value = sequence
        phase.value = PHASES.index('waiting')

    try:
        if factory is None:
            from collector import Collector
            collector = Collector(progress=lambda stage: setattr(phase, 'value', PHASES.index(stage)))
        else:
            collector = factory()
        sequence = 0
        publish({'ready': True}, sequence)
        while not stopping.value:
            if requested.value == sequence:
                time.sleep(.02)
                continue
            sequence = requested.value
            if stopping.value:
                break
            try:
                publish({'sample': collector.sample()}, sequence)
            except Exception as error:
                publish({'error': type(error).__name__ + ': ' + str(error)[:160]}, sequence)
    except Exception as error:
        publish({'error': type(error).__name__ + ': ' + str(error)[:160]}, 0)
    finally:
        if collector is not None:
            collector.close()


class IsolatedCollector:
    def __init__(self, factory=None, startup_timeout=45, sample_timeout=15, sensors=True, cancel_event=None, temperature_sampler=None):
        context = mp.get_context('spawn')
        self.buffer = context.RawArray(ctypes.c_ubyte, PAYLOAD_LIMIT + 4)
        # Lock-free aligned 32-bit counters on Windows x64. A terminated child
        # cannot strand an interprocess Event/Condition mutex in its locked state.
        self.requested = context.Value(ctypes.c_uint32, 0, lock=False)
        self.ready = context.Value(ctypes.c_uint32, 0xffffffff, lock=False)
        self.stopping = context.Value(ctypes.c_uint32, 0, lock=False)
        self.sequence = 0
        self.phase = context.Value(ctypes.c_int, 0, lock=False)
        self.timeout = sample_timeout
        self.cancel_event = cancel_event
        self.closed = False
        self.owns_temperatures = temperature_sampler is None
        self.temperatures = temperature_sampler if temperature_sampler is not None else TemperatureSampler(autostart=sensors)
        self.process = context.Process(target=run_worker, name='resource-sampling-worker',
            args=(self.buffer, self.requested, self.ready, self.stopping, self.phase, factory), daemon=True)
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
        # committing its sequence. One outstanding request keeps the buffer
        # immutable while the parent reads it; crashes cannot strand a mutex.
        deadline = time.monotonic() + timeout
        while self.ready.value != self.sequence:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise InterruptedError('Telemetry collection was stopped')
            if self.process.exitcode is not None:
                raise RuntimeError(f'Telemetry worker exited ({self.process.exitcode})')
            if time.monotonic() >= deadline:
                raise TimeoutError('Telemetry worker stopped responding: ' + PHASES[self.phase.value])
            time.sleep(min(.02, max(0, deadline - time.monotonic())))
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
        self.sequence = (self.sequence + 1) % 0xffffffff
        self.requested.value = self.sequence
        sample = self.receive(self.timeout).get('sample')
        if not isinstance(sample, dict):
            raise RuntimeError('Telemetry worker returned no sample')
        sample['system']['temperatures'] = self.temperatures.sample()
        return sample

    def close(self):
        if self.closed:
            return
        self.stopping.value = 1
        try:
            if self.process.pid is not None:
                self.process.join(timeout=1)
                if self.process.is_alive():
                    # Only the dedicated sampler owned by this instance. It never
                    # owns a governor lease or runs user workloads.
                    self.process.terminate()
                    self.process.join(timeout=3)
                if self.process.is_alive():
                    raise WorkerCleanupPending(self)
            self.closed = True
        except OSError as error:
            # Keep the owned process handle available to the parent's retry
            # loop, including when cleanup fails during initialization.
            raise WorkerCleanupPending(self) from error
        finally:
            if self.owns_temperatures:
                self.temperatures.close()
