"""Small private collection intent, separate from measurements and leases."""
import json
import os
from pathlib import Path
import uuid


def write_intent(path, session, collecting):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps({'session': session, 'collecting': bool(collecting)}), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_intent(path, session, fallback):
    try:
        with Path(path).open('rb') as stream:
            data = json.loads(stream.read(4096))
        if data.get('session') == session and isinstance(data.get('collecting'), bool):
            return data['collecting']
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return fallback
