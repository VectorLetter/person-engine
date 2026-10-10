"""Portable kernel writer lease; admission of other writers belongs to the caller."""
from __future__ import annotations
import fcntl,json,os,socket,sys,subprocess,uuid
from pathlib import Path
from .core_io import FoundationError,atomic_json,now
def process_info(pid):
    try:
        result = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'lstart=', '-o', 'command='], capture_output=True, text=True, errors='replace', timeout=5)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return {'executable': sys.executable, 'start_info': 'unavailable'}

def inspect(workspace):
    directory = Path(workspace) / 'state'
    inode, meta = (directory / 'writer.lock', directory / 'writer_owner.json')
    owner = None
    if meta.exists():
        try:
            owner = json.loads(meta.read_text())
        except (ValueError, OSError):
            owner = {'invalid_metadata': True}
    if not inode.exists():
        return {'state': 'STALE_METADATA' if owner else 'UNLOCKED', 'owner': owner, 'lease': 'kernel flock', 'repair_performed': False}
    fd = os.open(inode, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'HELD', 'owner': owner, 'lease': 'kernel flock', 'repair_performed': False}
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    stale = owner and (owner.get('invalid_metadata') or owner.get('status') != 'released')
    return {'state': 'STALE_METADATA' if stale else 'UNLOCKED', 'owner': owner, 'lease': 'kernel flock; stale metadata is not a live lease', 'repair_performed': False}

class WriterLock:

    def __init__(self, workspace, purpose):
        self.workspace, self.purpose = (Path(workspace).resolve(), purpose)
        self.fd = None
        self.owner = None

    def acquire(self):
        if self.fd is not None:
            raise FoundationError('LOCK_REENTRY', '当前锁已持有')
        directory = self.workspace / 'state'
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / 'writer.lock', os.O_RDWR | os.O_CREAT, 384)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise FoundationError('WRITER_BUSY', '已有写入任务，请等待其结束后重试')
        self.fd = fd
        self.owner = {'token': uuid.uuid4().hex, 'pid': os.getpid(), 'process_info': process_info(os.getpid()), 'host': socket.gethostname(), 'purpose': self.purpose, 'workspace': str(self.workspace), 'acquired_at': now(), 'heartbeat_at': now(), 'status': 'held'}
        try:
            atomic_json(directory / 'writer_owner.json', self.owner)
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            self.fd = None
            raise
        return self

    def assert_owned(self):
        if self.fd is None or self.owner['pid'] != os.getpid():
            raise FoundationError('LOCK_REQUIRED', '必须由当前进程持有统一writer锁')
        return self

    def heartbeat(self):
        self.assert_owned()
        self.owner['heartbeat_at'] = now()
        atomic_json(self.workspace / 'state/writer_owner.json', self.owner)

    def release(self):
        self.assert_owned()
        try:
            self.owner.update(status='released', released_at=now())
            atomic_json(self.workspace / 'state/writer_owner.json', self.owner)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
