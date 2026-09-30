"""Atomic files, an OS-held job lock, and immutable report versions."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class JobAlreadyRunning(RuntimeError):
    pass


@contextmanager
def job_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'job.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise JobAlreadyRunning('已有日报任务正在运行') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def read_index(root):
    path = Path(root) / 'reports/index.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version': 1, 'dates': {}, 'latest': None}


def publish_reports(root, reports):
    """The sole commit point is the atomic index replacement, for the entire run."""
    root = Path(root)
    index = read_index(root)
    run_id = uuid.uuid4().hex
    for report in reports:
        relative = Path(report['date']) / run_id
        directory = root / 'reports' / relative
        atomic_json(directory / 'report.json', report)
        atomic_text(directory / 'report.md', report['markdown'])
        atomic_text(directory / 'brief.txt', report['brief'] + '\n')
        index['dates'][report['date']] = (relative / 'report.json').as_posix()
    index['latest'] = max(index['dates'], default=None)
    atomic_json(root / 'reports/index.json', index)
    return index
