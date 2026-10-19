from types import SimpleNamespace as NS
import threading
import time
import unittest
from unittest.mock import patch

from system_metrics import SystemInfoSampler, connection_kind, traffic_delta


class SystemMetricsTests(unittest.TestCase):
    def test_traffic_converts_bytes_per_second_to_decimal_megabits(self):
        self.assertEqual(traffic_delta(NS(bytes_recv=100, bytes_sent=200),
            NS(bytes_recv=2_000_100, bytes_sent=1_000_200), 2), (8, 4))

    def test_counter_reset_and_warmup_are_missing_not_idle_or_spikes(self):
        current = NS(bytes_recv=100, bytes_sent=200)
        self.assertEqual(traffic_delta(None, current, 1), (None, None))
        self.assertEqual(traffic_delta(NS(bytes_recv=200, bytes_sent=100), current, 1), (None, None))

    def test_connection_type_uses_windows_medium_not_localized_adapter_name(self):
        self.assertEqual(connection_kind({'medium': 'Native 802.11', 'name': '인터넷'}), 'wireless')
        self.assertEqual(connection_kind({'medium': '802.3', 'name': 'Wi-Fi'}), 'wired')
        self.assertEqual(connection_kind({'medium': '802.3', 'virtual': True}), 'virtual')
        self.assertEqual(connection_kind(None), 'disconnected')

    def reader(self):
        reader = SystemInfoSampler.__new__(SystemInfoSampler)
        reader._lock = threading.Lock()
        reader._metadata = {'name': 'Ethernet', 'medium': '802.3', 'link_mbps': 1000}
        reader._updated = time.monotonic()
        reader._hardware = {}
        reader._previous = ('Old Wi-Fi', time.monotonic()-1, NS(bytes_recv=100_000_000, bytes_sent=200_000_000))
        return reader

    @patch('system_metrics.psutil.net_if_stats', return_value={'Ethernet': NS(isup=True)})
    @patch('system_metrics.psutil.net_io_counters', return_value={'Ethernet': NS(bytes_recv=1000, bytes_sent=2000)})
    def test_route_change_primes_new_interface_without_comparing_old_counters(self, counters, stats):
        result = self.reader().sample()['network']
        self.assertEqual(result['kind'], 'wired')
        self.assertIsNone(result['download_mbps'])
        self.assertEqual(result['status'], 'starting')

    def test_expired_inventory_does_not_label_old_adapter_as_active(self):
        reader = self.reader()
        reader._updated -= 60
        result = reader.sample()['network']
        self.assertEqual(result['kind'], 'unknown')
        self.assertIsNone(result['download_mbps'])
        self.assertIsNone(reader._previous)

    @patch('system_metrics.psutil.net_if_stats', return_value={'Ethernet': NS(isup=False)})
    @patch('system_metrics.psutil.net_io_counters', return_value={})
    def test_disconnected_adapter_never_shows_old_speed(self, counters, stats):
        result = self.reader().sample()['network']
        self.assertEqual(result['status'], 'disconnected')
        self.assertIsNone(result['download_mbps'])


if __name__ == '__main__':
    unittest.main()
