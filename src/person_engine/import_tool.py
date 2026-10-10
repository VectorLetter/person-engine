"""Explicit JPEG/PNG copy into an existing API workspace; no database or API mutation.

All path components are opened without following symlinks. Atomic link publication
provides no-overwrite semantics even for competing import attempts on one target.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

MAX_BYTES = 64 * 1024 * 1024


class ImportFailure(ValueError):
    """Safe, path-free CLI error code."""


def parts(value):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise ImportFailure('UNSAFE_PATH')
    components = value.split('/')
    if any(x in ('', '.', '..') for x in components):
        raise ImportFailure('UNSAFE_PATH')
    return components


def directory(stack, path, parent=None, create=False):
    if create:
        try:
            os.mkdir(path, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


def absolute_directory(stack, value):
    path = Path(value).absolute()
    fd = directory(stack, '/')
    for part in path.parts[1:]:
        parts(part)
        fd = directory(stack, part, fd)
    return fd


def read_file(parent, name, limit=MAX_BYTES, single_link=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (single_link and before.st_nlink != 1):
            raise ImportFailure('UNSAFE_FILE')
        if before.st_size > limit:
            raise ImportFailure('CONTENT_LIMIT')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(data) > limit:
        raise ImportFailure('CONTENT_LIMIT')
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ImportFailure('SOURCE_CHANGED')
    return data


def copy_import(root, source_directory, source_root, source, destination=None):
    """Return a path-free receipt. source_directory is the API's opaque hint.

    Does not create/adopt workspaces. It validates their public markers, then
    modifies only their imports directory. Trusted local operator, POSIX host.
    """
    hint = parts(source_directory)
    if len(hint) != 2 or hint[-1] != 'imports':
        raise ImportFailure('INVALID_IMPORT_DIRECTORY')
    source_parts = parts(source)
    with ExitStack() as stack:
        service = absolute_directory(stack, root)
        if json.loads(read_file(service, 'api-root.json', 4096)) != {'schema': 'local-fastapi-root/v1'}:
            raise ImportFailure('INVALID_SERVICE_ROOT')
        workspace = directory(stack, hint[0], service)
        marker = json.loads(read_file(workspace, 'workspace.json', 4096))
        if not isinstance(marker, dict) or marker.get('contract') != 'person-engine-public-v0/1' or not marker.get('workspace_id', '').startswith('ws_'):
            raise ImportFailure('INVALID_WORKSPACE')
        imports = directory(stack, 'imports', workspace)
        origin = absolute_directory(stack, source_root)
        for component in source_parts[:-1]:
            origin = directory(stack, component, origin)
        data = read_file(origin, source_parts[-1])
        if data.startswith(b'\xff\xd8\xff'):
            suffix, media_type = '.jpg', 'image/jpeg'
        elif data.startswith(b'\x89PNG\r\n\x1a\n'):
            suffix, media_type = '.png', 'image/png'
        else:
            raise ImportFailure('UNSUPPORTED_MEDIA_JPEG_PNG_ONLY')
        digest = hashlib.sha256(data).hexdigest()
        target = destination if destination is not None else digest + suffix
        target_parts = parts(target)
        parent = imports
        for component in target_parts[:-1]:
            parent = directory(stack, component, parent, create=True)
        name = target_parts[-1]
        receipt = {'source_reference': source, 'source_path': target, 'content_sha256': digest,
                   'bytes': len(data), 'media_type': media_type}

        def existing():
            try:
                content = read_file(parent, name, single_link=True)
            except ImportFailure as error:
                if str(error) != 'CONTENT_LIMIT':
                    raise
                return {**receipt, 'status': 'CONFLICT'}
            return {**receipt, 'status': 'REUSED' if content == data else 'CONFLICT'}

        try:
            return existing()
        except FileNotFoundError:
            pass
        temporary = '.import-' + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            except FileExistsError:
                return existing()
            # Only the helper's temporary copy is unlinked, never the source.
            os.unlink(temporary, dir_fd=parent)
            temporary = None
            os.fsync(parent)
            return {**receipt, 'status': 'COPIED'}
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=parent)


def main():
    parser = argparse.ArgumentParser(description='Explicit copy into existing workspace imports; JPEG/PNG only')
    parser.add_argument('--root', required=True, type=Path, help='Dedicated API service root')
    parser.add_argument('--source-directory', required=True, help='Opaque source_directory returned by workspace API')
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--source', required=True, help='Source-root-relative input')
    parser.add_argument('--destination', help='Optional imports-relative destination; never overwritten')
    args = parser.parse_args()
    try:
        receipt = copy_import(**vars(args))
    except (OSError, ValueError, TypeError, KeyError) as error:
        code = str(error) if isinstance(error, ImportFailure) else 'IMPORT_IO_OR_WORKSPACE_ERROR'
        print(json.dumps({'error': {'code': code}}))
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 3 if receipt['status'] == 'CONFLICT' else 0


if __name__ == '__main__':
    raise SystemExit(main())
