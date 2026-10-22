import json
from pathlib import Path
import tempfile
import time
import unittest
from collector import aggregate_gpu, classify
from history import History
from server import csv_safe, make_insights, next_sample_deadline, stable_workload


def sample(ts, cpu=10, gpu=95, ram=40):
    return {'ts': ts, 'sample_duration_s': 3, 'system': {
        'cpu_pct': cpu, 'cpu_peak_core_pct': 99, 'ram_pct': ram, 'ram_available_gb': 20,
        'disk_read_mbps': 1, 'disk_write_mbps': 2, 'disks': [],
        'gpus': [{'util_pct': gpu, 'vram_pct': 30}]}, 'groups': [
            {'id': 'voice', 'name': '음성', 'cpu_pct': cpu, 'ram_gb': 2,
             'gpu_pct': gpu, 'io_read_mbps': 1, 'io_write_mbps': 2}]}


class MonitorTests(unittest.TestCase):
    def test_workload_requires_same_live_lease_throughout_sample(self):
        lease = {'stage': 'qwen-vision', 'device': 'cuda', 'owner_pid': 42,
                 'owner_created': 100.0, 'request_id': 'sample-1'}
        self.assertEqual(stable_workload(lease, dict(lease)),
                         {'stage': 'qwen-vision', 'owner_pid': 42, 'owner_created': 100.0})
        for key in lease:
            self.assertIsNone(stable_workload(lease, {**lease, key: None}))
            self.assertIsNone(stable_workload(lease, {**lease, key: 'changed'}))
        self.assertIsNone(stable_workload(lease, {**lease, 'orphaned': True}))
        self.assertIsNone(stable_workload(None, lease))
        self.assertIsNone(stable_workload(lease, None))

    def test_sample_work_does_not_extend_one_second_period(self):
        self.assertEqual(next_sample_deadline(10, 10.3), 11)
        self.assertEqual(next_sample_deadline(11, 11.4), 12)

    def test_slow_sample_has_no_catchup_queue(self):
        self.assertEqual(next_sample_deadline(10, 13.2), 13.2)

    def test_same_engine_sum_different_engine_max(self):
        rows = [{'engines': [{'key': 'gpu0-engine1', 'pct': 30}, {'key': 'gpu0-engine2', 'pct': 20}]},
                {'engines': [{'key': 'gpu0-engine1', 'pct': 40}, {'key': 'gpu0-engine2', 'pct': 10}]}]
        self.assertEqual(aggregate_gpu(rows), (70, 'gpu0-engine1'))

    def test_gpu_sum_cap(self):
        self.assertEqual(aggregate_gpu([{'engines': [{'key': 'x', 'pct': 120}]}])[0], 100)

    def test_voice_and_music_identification(self):
        self.assertEqual(classify('python.exe', command=[r'C:\projects\my-sia-voice-studio\production.py'])[2], 'voice')
        self.assertEqual(classify('python.exe', command=[r'C:\music-studio\server.py'])[2], 'music')

    def test_no_rea_substring_false_positive(self):
        self.assertNotEqual(classify('python.exe', command=['create.py'])[0], 'rea')

    def test_pdh_dwm_without_extension_has_plain_windows_identity(self):
        self.assertEqual(classify('dwm'), ('desktop-compositor', 'Windows 화면 표시', 'system'))
        self.assertEqual(classify('dwm.exe'), classify('dwm'))

    def test_csv_formula_is_escaped(self):
        self.assertEqual(csv_safe('=1+1'), "'=1+1")
        self.assertEqual(csv_safe('normal'), 'normal')

    def test_short_record_does_not_recommend_purchase(self):
        result = make_insights([sample(time.time(), 100, 100, 95)], {'alive': False})
        self.assertIn('기록을 모으고', result[0]['title'])
        self.assertFalse(any('지속해서' in x['title'] for x in result))

    def test_gpu_absence_preserved(self):
        s = sample(time.time(), gpu=None)
        result = make_insights([s], {'alive': False})
        self.assertFalse(any('집중적' in x['title'] for x in result))

    def test_history_averages_peaks_and_null(self):
        with tempfile.TemporaryDirectory() as temp:
            h = History(Path(temp) / 'test.db')
            t = time.time()
            h.record([sample(t-3, 10, None), sample(t, 30, None)])
            result = h.read(60)
            self.assertAlmostEqual(result['system'][0]['cpu_pct'], 20)
            self.assertEqual(result['system'][0]['cpu_peak'], 30)
            self.assertIsNone(result['system'][0]['gpu_pct'])
            self.assertAlmostEqual(result['groups'][0]['io_total_gb'], 18/1024)
            self.assertAlmostEqual(result['coverage']['duration_minutes'], .1)
            self.assertEqual(h.stats()['samples'], 1)

    def test_retention_removes_old_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            h = History(Path(temp) / 'test.db')
            h.record([sample(time.time() - 9*86400)])
            h.record([sample(time.time())])
            self.assertEqual(h.stats()['samples'], 1)


if __name__ == '__main__':
    unittest.main()
