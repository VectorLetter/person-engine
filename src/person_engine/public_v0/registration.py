"""Logical source bindings are independent of shared content bytes."""
import os
from pathlib import Path
import tempfile

from ..core_io import digest, file_sha, fsync_dir
from .store import CONTRACT, require, exact_keys, token, replay, record, meta, bump, hook

MAX_SOURCE_BYTES = 64 * 1024 * 1024


def install_bytes(target, data, *, fault=None):
    """Atomic, immutable create. Existing bytes must agree; never replace."""
    require(not target.parent.is_symlink() and not target.is_symlink(), 'UNSAFE_PATH', 'Unsafe output')
    if target.exists():
        require(target.read_bytes() == data, 'CONFLICT', 'Immutable output differs')
        return
    fd, temp = tempfile.mkstemp(prefix='.pending-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        hook(fault, 'before_output_commit')
        try:
            os.link(temp, target)
        except FileExistsError:
            require(target.read_bytes() == data, 'CONFLICT', 'Concurrent output differs')
        fsync_dir(target.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


def register(ws, request, source, *, fault=None):
    exact_keys(request, ('contract', 'workspace_id', 'request_id', 'source_namespace', 'source_ref',
                         'content_sha256', 'bytes', 'media_type', 'expected_revision'))
    require(request['contract'] == CONTRACT, 'INCOMPATIBLE_REQUEST', 'Unsupported registration contract')
    require(request['workspace_id'] == ws.workspace_id, 'WORKSPACE_MISMATCH', 'Wrong workspace')
    for key in ('request_id', 'source_namespace', 'source_ref'):
        token(request[key], key)
    sha = request['content_sha256']
    require(isinstance(sha, str) and len(sha) == 64 and all(x in '0123456789abcdef' for x in sha),
            'INVALID_REQUEST', 'Invalid content digest')
    require(type(request['bytes']) is int and 0 < request['bytes'] <= MAX_SOURCE_BYTES,
            'CONTENT_LIMIT', 'Unsupported source size')
    require(type(request['expected_revision']) is int and request['expected_revision'] >= 0,
            'INVALID_REQUEST', 'Invalid revision')
    require(request['media_type'] in ('image/jpeg', 'image/png'), 'UNSUPPORTED_MEDIA', 'V0 accepts JPEG/PNG')
    with ws.write() as c:
        previous = replay(c, 'register', request['request_id'], request)
        if previous is not None:
            return previous
        require(int(meta(c, 'revision')) == request['expected_revision'], 'STALE_SOURCE', 'Registration revision changed')
        bound = c.execute('SELECT * FROM asset WHERE namespace=? AND source_ref=?',
                          (request['source_namespace'], request['source_ref'])).fetchone()
        if bound:
            require((bound['content_sha256'], bound['bytes'], bound['media_type']) ==
                    (sha, request['bytes'], request['media_type']), 'CONFLICT', 'Source reference is already bound')
            result = {'contract': CONTRACT, 'workspace_id': ws.workspace_id, 'asset_id': bound['asset_id'],
                      'content_sha256': sha, 'registration_id': bound['registration_id'],
                      'registry_revision': int(meta(c, 'revision')), 'action': 'BOUND_REPLAY'}
        else:
            source = Path(source)
            require(not any(p.is_symlink() for p in (source.absolute(), *source.absolute().parents)),
                    'UNSAFE_PATH', 'Symlink source')
            require(source.is_file() and source.stat().st_size == request['bytes'], 'SOURCE_CHANGED', 'Source size differs')
            data = source.read_bytes()
            import hashlib
            require(hashlib.sha256(data).hexdigest() == sha and len(data) == request['bytes'], 'SOURCE_CHANGED', 'Source digest differs')
            require((request['media_type'] == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or
                    (request['media_type'] == 'image/jpeg' and data.startswith(b'\xff\xd8\xff')),
                    'UNSUPPORTED_MEDIA', 'Image header and declared type differ')
            install_bytes(ws.home / 'content' / sha, data)
            aid = 'asset_' + digest([ws.workspace_id, request['source_namespace'], request['source_ref']])
            rid = 'registration_' + digest([ws.workspace_id, request])
            c.execute('INSERT INTO asset VALUES(?,?,?,?,?,?,?)',
                      (aid, request['source_namespace'], request['source_ref'], sha, len(data), request['media_type'], rid))
            hook(fault, 'after_asset_insert', c)
            result = {'contract': CONTRACT, 'workspace_id': ws.workspace_id, 'asset_id': aid,
                      'content_sha256': sha, 'registration_id': rid, 'registry_revision': bump(c), 'action': 'REGISTERED'}
        result['receipt_digest'] = digest(result)
        return record(c, 'register', request['request_id'], request, result)
