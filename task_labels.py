"""Shared plain-language labels, including safe rendering of older records."""
import json
from app_paths import bundle_dir

_text = (bundle_dir() / 'static' / 'task-labels.mjs').read_text(encoding='utf-8').strip()
LABELS = json.loads(_text.removeprefix('export default ')[:-1])


def program_rule(program):
    return next((item for item in LABELS['programs'] if program.casefold() in [alias.casefold() for alias in item['aliases']]), None)


def friendly_program(program):
    rule = program_rule(program)
    return rule['name'] if rule else program


def friendly_task(program, task):
    rule = program_rule(program)
    if rule and (not task or '미확인' in task or task in ('3D 렌더링', 'GPU 연산', 'GPU 데이터 전송') or task.casefold() in [s.casefold() for s in rule['aliases']]):
        return rule['task']
    script = next((item for item in LABELS['scripts'] if item['programContains'] in program.casefold() and item['file'] == task), None)
    return script['task'] if script else task
