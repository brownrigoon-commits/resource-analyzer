from pathlib import Path
import tempfile
import unittest

from warning_history import WarningHistory, warning_reports


def snapshot(ts, cpu=75, cause='synthetic-ai'):
    return {'ts': ts, 'system': {'cpu_pct': cpu, 'ram_pct': 30, 'disk_busy_pct': 0,
                                'gpus': [{'name': 'Synthetic GPU', 'util_pct': 0}]},
            'groups': [{'id': cause, 'name': cause, 'cpu_pct': 50, 'ram_gb': 1, 'gpu_pct': 0}]}


class WarningHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'warning-events.sqlite3'
        self.history = WarningHistory(self.path)

    def test_recovery_keeps_occurrence_and_reason_after_reopen(self):
        self.history.record(snapshot(1000))
        self.history.record(snapshot(1001, cpu=10))
        events = WarningHistory(self.path).read()
        self.assertEqual(events['total'], 1)
        event = events['events'][0]
        self.assertEqual(event['started_at'], 1000)
        self.assertEqual(event['ended_at'], 1001)
        self.assertEqual(event['end_reason'], 'recovered')
        self.assertIn('synthetic-ai', event['evidence'])
        self.assertEqual(events['active_count'], 0)

    def test_continuous_warning_updates_peak_without_duplicate_rows(self):
        for ts, value in [(1000, 75), (1001, 85), (1002, 72)]:
            self.history.record(snapshot(ts, value))
        event = self.history.read()['events'][0]
        self.assertEqual(self.history.read()['total'], 1)
        self.assertEqual(event['sample_count'], 3)
        self.assertEqual(event['numeric_value'], 85)
        self.assertEqual(event['last_value'], '72.0%')
        self.assertEqual(event['last_seen'], 1002)
        self.assertEqual(event['peak_at'], 1001)

    def test_level_or_dominant_program_change_creates_another_record(self):
        self.history.record(snapshot(1000, 75))
        self.history.record(snapshot(1001, 95))
        self.history.record(snapshot(1002, 95, 'synthetic-renderer'))
        data = self.history.read()
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['active_count'], 1)
        self.assertEqual(data['events'][0]['level'], 'high')
        self.assertIn('synthetic-renderer', data['events'][0]['evidence'])
        self.assertEqual(data['events'][1]['end_reason'], 'changed')

    def test_missing_measurement_and_restart_do_not_claim_recovery(self):
        self.history.record(snapshot(1000))
        self.history.record(snapshot(1001, cpu=None))
        self.assertEqual(self.history.read()['events'][0]['end_reason'], 'measurement_gap')
        self.history.record(snapshot(1002))
        reopened = WarningHistory(self.path)
        self.assertEqual(reopened.read()['events'][0]['end_reason'], 'restart')
        reopened.record(snapshot(1003))
        self.assertEqual(reopened.read()['total'], 3)

    def test_stale_and_stopped_collection_preserve_records(self):
        self.history.record(snapshot(1000))
        stale = snapshot(1001, 99)
        stale['stale'] = True
        self.history.record(stale)
        self.assertEqual(self.history.read()['total'], 1)
        self.assertEqual(self.history.read()['active_count'], 0)
        self.history.record(snapshot(1002))
        self.history.pause('collection_stopped')
        self.assertEqual(self.history.read()['total'], 2)

    def test_pagination_is_stable_and_records_are_not_deleted_after_seven_days(self):
        for index in range(7):
            ts = 1000 + index * 86400 * 8
            self.history.record(snapshot(ts))
            self.history.record(snapshot(ts + 1, 0))
        first = self.history.read(limit=3)
        second = self.history.read(limit=3, before=first['next_before'])
        third = self.history.read(limit=3, before=second['next_before'])
        ids = [r['event_id'] for page in (first, second, third) for r in page['events']]
        self.assertEqual(len(set(ids)), 7)
        self.assertIsNone(third['next_before'])
        self.assertEqual(third['total'], 7)
        self.assertEqual(third['events'][-1]['started_at'], 1000)

    def test_duplicate_sample_is_not_counted_twice(self):
        self.history.record(snapshot(1000))
        self.history.record(snapshot(1000))
        self.assertEqual(self.history.read()['events'][0]['sample_count'], 1)

    def test_disk_io_evidence_and_gpu_sensor_identity_remain_qualified(self):
        data = snapshot(1000, 0)
        data['system'].update(disk_busy_pct=99, gpus=[
            {'name': 'Cool busy GPU', 'util_pct': 95, 'temp_c': 40},
            {'name': 'Hot idle GPU', 'util_pct': 0, 'temp_c': 95}])
        data['groups'][0].update(io_read_mbps=5, io_write_mbps=10)
        reports = {r['id']: r for r in warning_reports(data)[1]}
        self.assertIn('Hot idle GPU', reports['gpu-temperature']['evidence'])
        self.assertIn('Cool busy GPU', reports['gpu-usage']['note'])
        self.assertIn('논리 I/O', reports['disk-usage']['evidence'])
        self.assertIn('확정할 수 없습니다', reports['disk-usage']['note'])


if __name__ == '__main__':
    unittest.main()
