from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest

from task_context import TaskContext, process_role, recent_operation, tool_label

SID = '12345678-1234-1234-1234-123456789abc'


def line(ts, kind='assistant', parts=None, **extra):
    return json.dumps({'sessionId': SID, 'type': kind, 'timestamp': datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                       'message': {'content': parts or []}, **extra}).encode()


def tool(ts=1000, name='Edit', **inputs):
    return line(ts, parts=[{'type': 'tool_use', 'id': 't1', 'name': name, 'input': inputs}])


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = TaskContext(self.temp.name)
        self.process = {'pid': 42, 'created_at': 900, 'process_role': 'claude-session', 'ppid': 1}
        sessions = self.reader.root / 'sessions'
        sessions.mkdir(parents=True)
        self.data = {'pid': 42, 'procStart': str((11644473600 + 900) * 10000000), 'name': '예시 음성 제작',
                     'sessionId': SID, 'cwd': r'C:\projects\example-voice'}
        self.session_file = sessions / '42.json'
        self.session_file.write_text(json.dumps(self.data), encoding='utf-8')
        self.transcript = self.reader.root / 'projects' / re.sub('[^a-zA-Z0-9]', '-', self.data['cwd']) / (SID + '.jsonl')
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_bytes(tool(file_path=r'C:\private\voice.py', old_string='PRIVATE_OLD', new_string='PRIVATE_NEW') + b'\n')

    def test_matching_live_session_has_title_and_actual_tool(self):
        context = self.reader.session(self.process, 1001)
        self.assertEqual(context['title'], '예시 음성 제작')
        self.assertEqual(context['operation'], '파일 수정: voice.py 진행')
        self.assertNotIn('PRIVATE_', repr(self.reader.cache))
        self.assertNotIn('C:\\private', repr(context))

    def test_reused_pid_or_wrong_pid_cannot_supply_context(self):
        self.assertIsNone(self.reader.session({**self.process, 'created_at': 901}, 1001))
        self.data['pid'] = 43
        self.session_file.write_text(json.dumps(self.data), encoding='utf-8')
        self.assertIsNone(self.reader.session(self.process, 1001))

    def test_completed_and_expired_work_is_not_shown_as_current(self):
        done = line(1002, 'user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'PRIVATE_RESULT'}])
        self.transcript.write_bytes(self.transcript.read_bytes() + done + b'\n')
        self.assertEqual(self.reader.session(self.process, 1003)['operation'], '최근 파일 수정: voice.py')
        self.assertNotIn('operation', self.reader.session(self.process, 1031))
        self.assertNotIn('PRIVATE_RESULT', repr(self.reader.cache))

    def test_pending_work_expires_even_when_file_does_not_change(self):
        self.assertIn('operation', self.reader.session(self.process, 1001))
        self.assertNotIn('operation', self.reader.session(self.process, 1301))

    def test_partial_json_and_other_sessions_or_subagents_are_ignored(self):
        lines = [b'incomplete', line(1000, parts=[{'type':'tool_use','name':'Edit','input':{'file_path':'wrong.py'}}], sessionId='other'),
                 line(1000, parts=[{'type':'tool_use','name':'Edit','input':{'file_path':'side.py'}}], isSidechain=True)]
        self.assertIsNone(recent_operation(lines, SID, 1001))

    def test_invalid_session_path_does_not_read_an_arbitrary_transcript(self):
        self.data['sessionId'] = '../../private'
        self.session_file.write_text(json.dumps(self.data), encoding='utf-8')
        self.assertNotIn('operation', self.reader.session(self.process, 1001))
        self.assertFalse(self.reader.cache)

    def test_missing_corrupt_or_oversized_metadata_is_optional(self):
        for payload in ('{broken', ' ' * 65537, '[]'):
            self.session_file.write_text(payload, encoding='utf-8')
            self.assertIsNone(self.reader.session(self.process, 1001))
        self.session_file.unlink()
        self.assertIsNone(self.reader.session(self.process, 1001))

    def test_child_gets_only_matching_live_ancestor_session(self):
        child = {'pid': 50, 'ppid': 42, 'created_at': 950}
        unrelated = {'pid': 51, 'ppid': 42, 'created_at': 800}
        pool = {42: dict(self.process), 50: child, 51: unrelated}
        self.reader.attach(pool, 1001)
        self.assertEqual(child['task_context']['title'], '예시 음성 제작')
        self.assertNotIn('task_context', unrelated)

    def test_process_roles_and_commands_do_not_retain_arguments(self):
        self.assertEqual(process_role('claude.exe', ['claude.exe', '--type=gpu-process', '--secret=PRIVATE']), 'gpu-process')
        self.assertEqual(process_role('claude.exe', ['claude.exe', '--output-format', 'stream-json']), 'claude-session')
        self.assertEqual(process_role('other.exe', ['other.exe', '--type=gpu-process']), '')
        self.assertEqual(tool_label('PowerShell', {'command': 'python -m unittest PRIVATE_ARGUMENT'}), '테스트 실행')
        self.assertEqual(tool_label('Bash', {'command': 'echo PRIVATE_ARGUMENT'}), '명령 실행')
        self.assertEqual(tool_label('Edit', {'file_path': r'C:\secret\.env'}), '파일 수정: 비공개 설정')


if __name__ == '__main__':
    unittest.main()
