import ctypes as ct
import time
import unittest
from unittest.mock import Mock, patch

from gpu_metrics import GpuSampler, _PdhItem, _PDH_MORE_DATA
from windows_gpu import aggregate_engines, aggregate_memory, luid_key


LUID_A = "0x00000000_0x00000a01"
LUID_B = "0x00000000_0x00000b02"
GIB = 1024 ** 3


def engine(pid, luid=LUID_A, index=0, kind="3D", physical=0):
    return f"pid_{pid}_luid_{luid}_phys_{physical}_eng_{index}_engtype_{kind}"


def memory(luid=LUID_A, physical=0):
    return f"luid_{luid}_phys_{physical}"


def adapter(luid=LUID_A, vendor=0x1002, capacity=8 * GIB):
    return {"id": f"gpu-luid-{luid}", "adapter_luid": luid, "name": "Example GPU",
            "vendor_id": vendor, "dedicated_bytes": capacity, "shared_bytes": 16 * GIB}


def sampler(adapters=None):
    """No driver calls or subprocesses in the regression suite."""
    with patch("gpu_metrics.os.name", "posix"):
        result = GpuSampler()
    result._windows_adapters = adapters if adapters is not None else [adapter()]
    result._windows_errors = []
    result._pdh_error = None
    result._pdh = Mock()
    result._pdh.PdhCollectQueryData.return_value = 0
    result._query = ct.c_void_p(1)
    result._counter = ct.c_void_p(2)
    result._memory_counter = ct.c_void_p(3)
    result._shared_counter = ct.c_void_p(4)
    result._pdh_last_collect = time.monotonic() - 1
    return result


class WindowsAggregationTests(unittest.TestCase):
    def test_sum_processes_on_same_engine_then_choose_busiest(self):
        processes, utilization = aggregate_engines([
            (engine(11), 30), (engine(22), 25),
            (engine(11, index=1, kind="Copy"), 42),
            (engine(11, LUID_B), 80),
        ])
        self.assertEqual(utilization, {LUID_A: 55, LUID_B: 80})
        self.assertEqual(processes[11]["gpu_pct"], 80)
        self.assertEqual(processes[22]["gpu_pct"], 25)

    def test_duplicate_instances_are_not_added_and_pid_zero_counts_for_device(self):
        processes, utilization = aggregate_engines([
            (engine(11), 30), (engine(11) + "#1", 40),
            (engine(0), 20), (engine(22), 60),
        ])
        self.assertEqual(utilization[LUID_A], 100)
        self.assertEqual(processes[11]["gpu_pct"], 40)
        self.assertNotIn(0, processes)
        self.assertEqual(len(processes[11]["engines"]), 1)

    def test_malformed_invalid_or_absent_counter_is_not_idle(self):
        processes, utilization = aggregate_engines([
            ("unrelated", 12), (engine(1), float("nan")),
            (engine(2), float("inf")), (engine(3), -1), (engine(4), None),
        ])
        self.assertEqual(processes, {})
        self.assertEqual(utilization, {})
        self.assertEqual(aggregate_engines([]), ({}, {}))
        self.assertEqual(aggregate_engines([(engine(1), 0)])[1][LUID_A], 0)

    def test_hex_case_padding_and_signed_luid_use_same_identity(self):
        _, utilization = aggregate_engines([(engine(1, "0X0_0XA01"), 17)])
        self.assertEqual(utilization, {LUID_A: 17})
        self.assertEqual(luid_key(-1, 3), "0xffffffff_0x00000003")

    def test_adapter_memory_deduplicates_physical_instances_and_separates_cards(self):
        result = aggregate_memory([
            (memory(), GIB), (memory() + "#1", GIB),
            (memory(physical=1), 2 * GIB), (memory(LUID_B), GIB / 2),
            ("pid_100_" + memory(), 10 * GIB),  # process memory is not total memory
        ])
        self.assertEqual(result, {LUID_A: 3 * GIB, LUID_B: GIB / 2})

    def test_memory_unknown_is_not_zero_and_valid_zero_stays_zero(self):
        self.assertEqual(aggregate_memory([(memory(), None),
                                          (memory(), float("nan")),
                                          (memory(), -1)]), {})
        self.assertEqual(aggregate_memory([(memory(), 0)]), {LUID_A: 0})


class WindowsSamplerTests(unittest.TestCase):
    def test_counter_mapping_uses_luid_even_when_adapter_names_match(self):
        with sampler([adapter(LUID_B), adapter(LUID_A)]) as gpu:
            gpu._pdh_collected = True
            gpu._windows_utilization = {LUID_A: 70, LUID_B: 2}
            gpu._read_counter = Mock(side_effect=[
                ([(memory(LUID_A), 4 * GIB), (memory(LUID_B), GIB)], True, []),
                ([(memory(LUID_A), GIB / 4)], True, []),
            ])
            devices, errors = gpu._sample_windows()
            self.assertEqual(errors, [])
            self.assertEqual([d["util_pct"] for d in devices], [2, 70])
            self.assertEqual([d["vram_used_gb"] for d in devices], [1, 4])
            self.assertEqual(devices[1]["vram_pct"], 50)
            self.assertEqual(devices[1]["shared_used_gb"], 0.25)
            self.assertIsNone(devices[0]["shared_used_gb"])
            self.assertTrue(all(d["temp_c"] is None for d in devices))

    def test_failed_collection_discards_prior_utilization_and_memory(self):
        with sampler() as gpu:
            gpu._windows_utilization = {LUID_A: 70}
            gpu._pdh_collected = True
            gpu._pdh.PdhCollectQueryData.return_value = 0x800007D5
            result = gpu.sample()
            device = result["gpus"][0]
            self.assertEqual(device["name"], "Example GPU")
            self.assertEqual(device["vram_total_gb"], 8)
            self.assertIsNone(device["util_pct"])
            self.assertIsNone(device["vram_used_gb"])
            self.assertFalse(result["available"])
            self.assertIsNone(gpu._pdh_last_collect)

    def test_failed_engine_read_can_still_report_valid_adapter_memory(self):
        with sampler() as gpu:
            gpu._read_counter = Mock(side_effect=[
                ([], False, ["Engine unavailable"]),
                ([(memory(), 2 * GIB)], True, []),
                ([], False, ["Shared memory unavailable"]),
            ])
            result = gpu.sample()
            device = result["gpus"][0]
            self.assertIsNone(device["util_pct"])
            self.assertEqual(device["vram_used_gb"], 2)
            self.assertTrue(result["available"])
            self.assertFalse(result["processes_available"])
            self.assertIn("Engine unavailable", result["errors"])
            self.assertFalse(any("NVIDIA" in error for error in result["errors"]))

    def test_rate_warmup_can_read_instantaneous_memory_without_inventing_utilization(self):
        with sampler() as gpu:
            gpu._pdh_last_collect = None
            gpu._read_counter = Mock(side_effect=[
                ([(memory(), GIB)], True, []), ([], True, []),
            ])
            result = gpu.sample()
            self.assertIsNone(result["gpus"][0]["util_pct"])
            self.assertEqual(result["gpus"][0]["vram_used_gb"], 1)
            self.assertFalse(result["processes_available"])
            self.assertEqual(gpu._read_counter.call_count, 2)

    def test_every_vendor_falls_back_when_nvidia_sources_are_absent(self):
        adapters = [adapter(LUID_A, 0x10DE), adapter(LUID_B, 0x8086),
                    adapter("0x00000000_0x00000c03", 0x1234)]
        with sampler(adapters) as gpu:
            gpu._pdh = None
            result = gpu.sample()
            self.assertEqual(len(result["gpus"]), 3)
            self.assertEqual({d["vendor_id"] for d in result["gpus"]}, {0x10DE, 0x8086, 0x1234})
            self.assertTrue(all(d["util_pct"] is None for d in result["gpus"]))

    def test_nvidia_fast_path_and_other_vendor_devices_coexist_without_duplicates(self):
        with sampler([adapter(LUID_A, 0x10DE), adapter(LUID_B, 0x1002)]) as gpu:
            gpu._pdh = None
            gpu._nvml = Mock()
            nvidia = {"id": "gpu-0", "name": "NVIDIA GPU", "source": "NVML",
                      "util_pct": 40, "vram_used_gb": 2}
            gpu._sample_nvml = Mock(return_value=([nvidia], []))
            result = gpu.sample()
            self.assertEqual(len(result["gpus"]), 2)
            self.assertIs(result["gpus"][0], nvidia)
            self.assertEqual(result["gpus"][1]["adapter_luid"], LUID_B)

    def test_partial_nvidia_enumeration_does_not_hide_the_second_card(self):
        with sampler([adapter(LUID_A, 0x10DE), adapter(LUID_B, 0x10DE)]) as gpu:
            gpu._pdh = None
            gpu._nvml = Mock()
            gpu._sample_nvml = Mock(return_value=([
                {"id": "gpu-0", "name": "Example GPU", "source": "NVML", "util_pct": 40},
            ], []))
            result = gpu.sample()
            self.assertEqual({device["adapter_luid"] for device in result["gpus"]},
                             {LUID_A, LUID_B})
            self.assertTrue(all(device["source"] == "Windows DXGI/PDH" for device in result["gpus"]))
            self.assertTrue(any("incomplete" in error for error in result["errors"]))

    def test_nvidia_unsupported_metric_recovers_from_windows_without_guessing_identity(self):
        for missing_metric in ("util_pct", "vram_used_gb"):
            with self.subTest(metric=missing_metric), sampler([adapter(LUID_A, 0x10DE)]) as gpu:
                gpu._nvml = Mock()
                nvidia = {"id": "gpu-7", "name": "Native name", "source": "NVML",
                          "util_pct": 40, "vram_used_gb": 2}
                nvidia[missing_metric] = None
                gpu._sample_nvml = Mock(return_value=([nvidia], ["NVML metric unsupported"]))
                gpu._read_counter = Mock(side_effect=[
                    ([(engine(1), 23)], True, []),
                    ([(memory(), GIB)], True, []), ([], True, []),
                ])
                device = gpu.sample()["gpus"][0]
                self.assertEqual(device["adapter_luid"], LUID_A)
                self.assertEqual(device["util_pct"], 23)
                self.assertEqual(device["vram_used_gb"], 1)
                self.assertEqual(device["source"], "Windows DXGI/PDH")

    def test_nvidia_valid_utilization_and_temperature_survive_pdh_failure_or_warmup(self):
        for pdh_state in ("failure", "warmup"):
            with self.subTest(pdh=pdh_state), sampler([adapter(LUID_A, 0x10DE)]) as gpu:
                gpu._nvml = Mock()
                nvidia = {"id": "gpu-7", "name": "Native name", "source": "NVML",
                          "util_pct": 40, "vram_used_gb": None, "temp_c": 62}
                gpu._sample_nvml = Mock(return_value=([nvidia], ["NVML memory unsupported"]))
                if pdh_state == "failure":
                    gpu._pdh.PdhCollectQueryData.return_value = 0x800007D5
                else:
                    gpu._pdh_last_collect = None
                    gpu._read_counter = Mock(side_effect=[
                        ([(memory(), GIB)], True, []), ([], True, []),
                    ])
                result = gpu.sample()
                self.assertEqual(result["gpus"], [nvidia])
                self.assertEqual(result["gpus"][0]["util_pct"], 40)
                self.assertEqual(result["gpus"][0]["temp_c"], 62)
                self.assertTrue(result["available"])
                self.assertIn("NVML memory unsupported", result["errors"])
                self.assertFalse(any("using Windows" in error for error in result["errors"]))

    def test_nvidia_metric_fallback_requires_valid_windows_values_for_every_card(self):
        with sampler([adapter(LUID_A, 0x10DE), adapter(LUID_B, 0x10DE)]) as gpu:
            gpu._nvml = Mock()
            native = [
                {"id": "gpu-0", "source": "NVML", "util_pct": 40,
                 "vram_used_gb": None, "temp_c": 62},
                {"id": "gpu-1", "source": "NVML", "util_pct": 20,
                 "vram_used_gb": 2, "temp_c": 50},
            ]
            gpu._sample_nvml = Mock(return_value=(native.copy(), ["NVML memory unsupported"]))
            gpu._read_counter = Mock(side_effect=[
                ([(engine(1), 23)], True, []),
                ([(memory(), GIB), (memory(LUID_B), 2 * GIB)], True, []),
                ([], True, []),
            ])
            self.assertEqual(gpu.sample()["gpus"], native)

    def test_nvidia_devices_remain_available_if_dxgi_cannot_enumerate_them(self):
        with sampler([]) as gpu:
            gpu._pdh = None
            gpu._nvml = Mock()
            nvidia = {"id": "gpu-0", "name": "NVIDIA GPU", "source": "NVML", "util_pct": 40}
            gpu._sample_nvml = Mock(return_value=([nvidia], []))
            self.assertEqual(gpu.sample()["gpus"], [nvidia])

    def test_zero_dedicated_capacity_is_not_a_percent_division_or_fake_usage(self):
        with sampler([adapter(capacity=0)]) as gpu:
            devices, _ = gpu._sample_windows()
            self.assertEqual(devices[0]["vram_total_gb"], 0)
            self.assertIsNone(devices[0]["vram_used_gb"])
            self.assertIsNone(devices[0]["vram_pct"])

    def test_pdh_rejects_invalid_status_but_accepts_new_data(self):
        with sampler() as gpu:
            names = [engine(1), engine(2), engine(3)]
            def formatted(counter, flags, size, count, buffer):
                ct.cast(size, ct.POINTER(ct.c_uint32))[0] = ct.sizeof(_PdhItem) * 3
                ct.cast(count, ct.POINTER(ct.c_uint32))[0] = 3
                if buffer is None:
                    return _PDH_MORE_DATA
                items = ct.cast(buffer, ct.POINTER(_PdhItem))
                for i, status in enumerate((0, 1, 0xC0000BC6)):
                    items[i].szName = names[i]
                    items[i].FmtValue.CStatus = status
                    items[i].FmtValue.doubleValue = i * 10
                return 0
            gpu._pdh.PdhGetFormattedCounterArrayW.side_effect = formatted
            rows, available, errors = gpu._read_counter(gpu._counter, "engine")
            self.assertTrue(available)
            self.assertEqual(errors, [])
            self.assertEqual(rows, [(names[0], 0), (names[1], 10)])


if __name__ == "__main__":
    unittest.main()
