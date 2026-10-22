"""Bounded local task metadata. Never retain prompts, tool inputs or commands."""
from datetime import datetime
import json
from pathlib import Path, PureWindowsPath
import re


def short(value, limit=60):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value).strip()
    if re.search(r'(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+)', value, re.I):
        return '비공개 작업'
    return value[:limit] + ('…' if len(value) > limit else '')


def process_role(name, command):
    if name.lower() not in ('claude.exe', 'codex.exe', 'chatgpt.exe', 'chrome.exe', 'msedge.exe'):
        return ''
    for arg in command[1:]:
        if arg in ('--type=gpu-process', '--type=renderer', '--type=utility'):
            return arg.split('=', 1)[1]
    return 'claude-session' if name.lower() == 'claude.exe' and '--output-format' in command else ''


def tool_label(name, inputs):
    labels = {'Read': '파일 읽기', 'Edit': '파일 수정', 'Write': '파일 작성',
              'Glob': '파일 찾기', 'Grep': '코드 검색', 'Agent': '하위 작업', 'Task': '하위 작업'}
    if name in labels:
        filename = short(PureWindowsPath(inputs.get('file_path', '')).name, 45)
        if re.search(r'(^\.env|token|secret|password|credential|api.?key)', filename, re.I):
            filename = '비공개 설정'
        return labels[name] + (': ' + filename if name in ('Read', 'Edit', 'Write') and filename else '')
    if name in ('Bash', 'PowerShell'):
        command = inputs.get('command')
        command = command.strip() if isinstance(command, str) else ''
        for pattern, label in [
            (r'^(?:python(?:\.exe)?|py)\s+-m\s+(?:pytest|unittest)\b', '테스트 실행'),
            (r'^node(?:\.exe)?\s+--test\b', '테스트 실행'),
            (r'^(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b', '앱 빌드'),
            (r'^git\s+push\b', 'GitHub 업로드'),
            (r'^git\s+(?:status|diff|log)\b', '변경 사항 확인'),
        ]:
            if re.search(pattern, command, re.I):
                return label
        return '명령 실행'
    return ''


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def recent_operation(lines, session_id, now):
    """Return only allowlisted tool labels, with completed work explicitly recent."""
    latest = None
    finished = set()
    for line in lines:
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get('sessionId') != session_id or event.get('isSidechain'):
                continue
            ts = timestamp(event.get('timestamp'))
            if ts is None or not 0 <= now - ts <= 300:
                continue
            message = event.get('message') or {}
            parts = message.get('content') if isinstance(message, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get('type') == 'tool_result':
                    finished.add(part.get('tool_use_id'))
                if event.get('type') == 'assistant' and part.get('type') == 'tool_use':
                    inputs = part.get('input')
                    label = tool_label(part.get('name'), inputs) if isinstance(inputs, dict) else ''
                    if label and (latest is None or ts >= latest[0]):
                        latest = (ts, part.get('id'), label)
        except (ValueError, TypeError):
            continue
    if latest is None:
        return None
    ts, tool_id, label = latest
    if tool_id in finished:
        return ('최근 ' + label, ts) if now - ts <= 30 else None
    return (label + ' 진행', ts)


class TaskContext:
    def __init__(self, home=None):
        self.root = (Path(home) if home is not None else Path.home()) / '.claude'
        self.cache = {}

    def session(self, process, now):
        pid, created = process.get('pid'), process.get('created_at')
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(created, (float, int)):
            return None
        path = self.root / 'sessions' / f'{pid}.json'
        try:
            stat = path.stat()
            if stat.st_size > 65536:
                return None
            with path.open('rb') as stream:
                data = json.loads(stream.read(65537).decode('utf-8-sig'))
            # Claude procStart is Windows FILETIME, not startedAt (the later session start).
            birth = int(data['procStart']) / 10_000_000 - 11644473600
            if data.get('pid') != pid or abs(birth - created) >= .01:
                return None
            title = short(data.get('name')) or short(PureWindowsPath(data.get('cwd', '')).name)
            if not title:
                return None
            result = {'title': title}
            sid, cwd = data.get('sessionId'), data.get('cwd')
            if not isinstance(sid, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', sid) or not isinstance(cwd, str):
                return result
            transcript = self.root / 'projects' / re.sub('[^a-zA-Z0-9]', '-', cwd) / (sid + '.jsonl')
            operation = self.operation(transcript, sid, now)
            if operation:
                result.update(operation=operation[0], observed_at=operation[1])
            return result
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return None

    def operation(self, path, sid, now):
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self.cache.get(path)
            if not cached or cached[0] != signature:
                with path.open('rb') as stream:
                    start = max(0, stat.st_size - 262144)
                    stream.seek(start)
                    lines = stream.read(262144).splitlines()
                if start:
                    lines = lines[1:]  # A partial first JSON object is not a record.
                operation = recent_operation(lines, sid, now)
                self.cache[path] = (signature, operation)
            operation = self.cache[path][1]
            if operation and 0 <= now - operation[1] <= (30 if operation[0].startswith('최근 ') else 300):
                return operation
            return None
        except OSError:
            return None

    def attach(self, processes, now):
        sessions = {}
        for process in processes.values():
            if process.get('process_role') == 'claude-session' and len(sessions) < 32:
                context = self.session(process, now)
                if context:
                    sessions[process['pid']] = context
        # Keep only metadata for current live sessions, with a strict cache bound.
        if len(self.cache) > 32:
            self.cache = {}
        if not sessions:
            return
        for process in processes.values():
            current = process
            seen = set()
            for _ in range(8):
                pid = current['pid']
                if pid in seen:
                    break
                seen.add(pid)
                if pid in sessions:
                    process['task_context'] = sessions[pid]
                    break
                parent = processes.get(current.get('ppid'))
                if not parent or not isinstance(parent.get('created_at'), (int, float)) or not isinstance(current.get('created_at'), (int, float)):
                    break
                if parent['created_at'] > current['created_at']:
                    break
                current = parent
