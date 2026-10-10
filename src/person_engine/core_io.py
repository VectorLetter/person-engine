'Step 1 IO: source-read-only SQLite views and durable artifact primitives.'

from __future__ import annotations

import contextlib

import hashlib

import json

import os

from pathlib import Path

import shutil

import sqlite3

import tempfile

from datetime import datetime, timezone

class FoundationError(RuntimeError):

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def now():
    return datetime.now(timezone.utc).isoformat()

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def fsync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)

def source_version(path):
    """Main DB + WAL bytes; never open SQLite on the source or touch its SHM."""
    path = Path(path)
    if Path(str(path) + '-journal').exists():
        raise FoundationError('SQLITE_JOURNAL', f'发现 journal，仅报告：{path}')
    if not path.is_file():
        raise FoundationError('MISSING_RESOURCE', f'缺少文件：{path}')
    return {suffix: file_sha(Path(str(path) + suffix)) for suffix in ('', '-wal') if Path(str(path) + suffix).exists()}

@contextlib.contextmanager
def read_snapshot(path):
    """Copy a stable main/WAL pair; all SQLite recovery/SHM IO is disposable.

    Refuse journals. Compare source bytes before/after copy and the copied bytes.
    This is not a substitute for a writer fence in a cross-resource backup.
    """
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix='pe_read_') as td:
        target = Path(td) / 'snapshot.db'
        before = source_version(path)
        for suffix in before:
            shutil.copyfile(Path(str(path) + suffix), Path(str(target) + suffix))
        if before != source_version(path) or any((file_sha(Path(str(target) + suffix)) != h for suffix, h in before.items())):
            raise FoundationError('SOURCE_CHANGED', '读取期间数据库发生变化，请重试')
        c = sqlite3.connect(target)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA query_only=ON')
            c.execute('PRAGMA foreign_keys=ON')
            yield c
        finally:
            c.close()

def sqlite_copy(source, destination):
    with read_snapshot(source) as src:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            if [r[0] for r in dst.execute('PRAGMA integrity_check')] != ['ok'] or dst.execute('PRAGMA foreign_key_check').fetchall():
                raise FoundationError('SNAPSHOT_INTEGRITY', '备份SQLite副本integrity/FK失败')
        finally:
            dst.close()
