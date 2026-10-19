import time
import unittest
from temperature_metrics import summarize, TemperatureSampler


class TemperatureTests(unittest.TestCase):
    def test_missing_sensor_is_not_zero_degrees(self):
        values = summarize({'sampled_at': 100, 'sensors': []}, now=100)
        self.assertTrue(all(v['temp_c'] is None for v in values.values()))

    def test_independent_categories_choose_highest_measured_value(self):
        values = summarize({'sampled_at': 100, 'sensors': [
            {'kind': 'cpu', 'name': 'CPU Package', 'temp_c': 63},
            {'kind': 'ram', 'name': 'DIMM 1', 'temp_c': 34},
            {'kind': 'ram', 'name': 'DIMM 2', 'temp_c': 37},
            {'kind': 'disk', 'name': 'NVMe', 'temp_c': 42}]}, now=100)
        self.assertEqual([values[k]['temp_c'] for k in ('cpu', 'ram', 'disk')], [63, 37, 42])
        self.assertIn('DIMM 1', values['ram']['details'])

    def test_stalled_bridge_does_not_leave_old_temperatures_visible(self):
        values = summarize({'sampled_at': 100, 'sensors': [{'kind': 'cpu', 'temp_c': 65}]}, now=110)
        self.assertIsNone(values['cpu']['temp_c'])
        self.assertEqual(values['cpu']['status'], 'stale')

    def test_invalid_and_nonfinite_readings_are_not_displayed(self):
        for value in (None, True, float('nan'), float('inf'), 255, -999, '42'):
            with self.subTest(value=value):
                result = summarize({'sampled_at': 100, 'sensors': [{'kind': 'cpu', 'temp_c': value}]}, now=100)
                self.assertIsNone(result['cpu']['temp_c'])

    def test_sampling_does_not_wait_for_hardware_initialization(self):
        reader = TemperatureSampler(autostart=False)
        start = time.monotonic()
        self.assertEqual(reader.sample()['cpu']['status'], 'starting')
        self.assertLess(time.monotonic() - start, .05)
        reader.close()


if __name__ == '__main__':
    unittest.main()
