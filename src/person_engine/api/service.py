"""Local orchestration only. Core owns all identity, revisions and receipts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import fcntl
import json
from pathlib import Path
import re
import uuid
from threading import RLock

from .. import public_v0 as core
from ..core_io import FoundationError, canonical, digest, now
from ..public_v0.store import safe_home, require, token, check_basis
from .errors import describe
from .synthetic_acceptance import SyntheticAcceptanceProvider


class Service:
    def __init__(self, root, provider=None):
        self.root = safe_home(root)
        # A dedicated adapter root is required; never adopt an existing core/private tree.
        marker = self.root / 'api-root.json'
        if self.root.exists() and any(self.root.iterdir()):
            require(marker.is_file() and not marker.is_symlink() and
                    json.loads(marker.read_text()) == {'schema': 'local-fastapi-root/v1'},
                    'INVALID_WORKSPACE', 'A dedicated API root is required')
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            marker.write_text(canonical({'schema': 'local-fastapi-root/v1'}) + '\n')
        safe_home(self.root / '.service.lock')
        self.lease = (self.root / '.service.lock').open('a')
        try:
            fcntl.flock(self.lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close()
            raise FoundationError('WRITER_BUSY', 'One service process per API root') from None
        self.workspaces = {}
        self.locks = {}
        self.guard = RLock()
        self.job_epoch = uuid.uuid4().hex
        self.jobs = {}
        self.job_payloads = {}
        self.provider = provider
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='person-engine-local')
        try:
            for folder in self.root.iterdir():
                if re.fullmatch(r'slot_[0-9a-f]{64}', folder.name):
                    ws = core.Workspace(folder)
                    require(ws.workspace_id not in self.workspaces, 'CONFLICT', 'Duplicate workspace identity')
                    self.workspaces[ws.workspace_id] = folder
                    self.locks[ws.workspace_id] = RLock()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.executor.shutdown(wait=True)
        fcntl.flock(self.lease.fileno(), fcntl.LOCK_UN)
        self.lease.close()

    def workspace(self, workspace_id):
        token(workspace_id)
        with self.guard:
            folder = self.workspaces.get(workspace_id)
        require(folder is not None, 'WORKSPACE_NOT_FOUND', 'Unknown workspace')
        ws = core.Workspace(folder)
        require(ws.workspace_id == workspace_id, 'WORKSPACE_MISMATCH', 'Workspace changed')
        return ws

    @contextmanager
    def mutation(self, workspace_id):
        ws = self.workspace(workspace_id)
        lock = self.locks[workspace_id]
        # Never block an HTTP worker on a long inference job.
        require(lock.acquire(blocking=False), 'WRITER_BUSY', 'Workspace busy')
        try:
            yield ws
        finally:
            lock.release()

    def create_workspace(self, key):
        with self.guard:
            folder = self.root / ('slot_' + digest(token(key)))
            ws = core.initialize(folder)  # Existing slot -> core WORKSPACE_EXISTS.
            (folder / 'imports').mkdir()
            self.workspaces[ws.workspace_id] = folder
            self.locks[ws.workspace_id] = RLock()
            return self.workspace_view(ws)

    def workspace_view(self, ws):
        created = (ws.home / 'workspace.json').stat().st_mtime
        return {'workspace_id': ws.workspace_id,
                'created_at': datetime.fromtimestamp(created, timezone.utc).isoformat(),
                'state': 'INITIALIZED', 'contract': core.CONTRACT, 'api_version': 'v1',
                'basis': ws.basis(), 'source_directory': ws.home.name + '/imports'}

    def source(self, ws, relative):
        p = Path(relative)
        require(not p.is_absolute() and '\\' not in relative and
                all(x not in ('', '.', '..') for x in relative.split('/')),
                'UNSAFE_PATH', 'Invalid source path')
        target = ws.home / 'imports' / p
        safe_home(target)  # Checks every component for symlinks before resolve.
        require(target.resolve().is_relative_to((ws.home / 'imports').resolve()) and
                (not target.exists() or target.is_file() and target.stat().st_nlink == 1),
                'UNSAFE_PATH', 'Unsafe source')
        return target

    def register(self, workspace_id, body, key):
        with self.mutation(workspace_id) as ws:
            source = self.source(ws, body.source_path)
            payload = body.model_dump(exclude={'source_path'})
            payload.update(contract=core.CONTRACT, workspace_id=workspace_id, request_id=key)
            with ws.read() as c:
                prior = c.execute("SELECT 1 FROM receipt WHERE kind='register' AND request_id=?", (key,)).fetchone()
            receipt = core.register(ws, payload, source)
            replay = prior is not None or receipt['action'] == 'BOUND_REPLAY'
            return {'asset_id': receipt['asset_id'], 'status': 'REPLAY' if replay else 'CREATED',
                    'replay': replay, 'receipt': receipt}

    def enqueue(self, workspace_id, body, key, request_id):
        ws = self.workspace(workspace_id)
        require(body.expected_basis.workspace_id == workspace_id, 'WORKSPACE_MISMATCH', 'Wrong basis workspace')
        require(len(body.asset_ids) == len(set(body.asset_ids)), 'INVALID_REQUEST', 'Duplicate asset IDs')
        with ws.read() as c:
            known = {r[0] for r in c.execute('SELECT asset_id FROM asset')}
        require(set(body.asset_ids) <= known, 'NOT_FOUND', 'Unknown workspace assets')
        jid = 'job_' + digest([self.job_epoch, workspace_id, key])
        payload = body.model_dump()
        with self.guard:
            if jid in self.jobs:
                require(self.job_payloads[jid] == payload, 'CONFLICT', 'Job key reused')
                return copy.deepcopy(self.jobs[jid]), True
            require(len(self.jobs) < 1024, 'JOB_LIMIT', 'Job history limit')
            job = {'job_id': jid, 'workspace_id': workspace_id, 'request_id': request_id,
                   'state': 'QUEUED', 'created_at': now(), 'started_at': None, 'finished_at': None,
                   'progress': 'queued', 'result_refs': {}, 'diagnostics': ['PUBLIC_CALIBRATION_GAP'], 'error': None}
            self.jobs[jid] = job
            self.job_payloads[jid] = payload
            result = copy.deepcopy(job)
            self.executor.submit(self.execute, jid, workspace_id, payload)
            return result, False

    def update_job(self, jid, **values):
        with self.guard:
            self.jobs[jid].update(values)

    def execute(self, jid, workspace_id, payload):
        self.update_job(jid, state='RUNNING', started_at=now(), progress='ingest')
        try:
            require(payload['policy'] is not None, 'CALIBRATION_REQUIRED', 'Policy required')
            require(self.provider is not None or not payload['asset_ids'], 'MODEL_UNAVAILABLE', 'Provider missing')
            with self.mutation(workspace_id) as ws:
                with ws.read() as c:
                    check_basis(c, payload['expected_basis'])
                current = payload['expected_basis']
                for asset_id in payload['asset_ids']:
                    core.ingest(ws, asset_id, self.provider, 'ingest_' + digest([jid, asset_id]), current)
                    current = ws.basis()
                self.update_job(jid, progress='match')
                run = core.match(ws, 'match_' + digest(jid), current, payload['policy'])
            self.update_job(jid, state='SUCCEEDED', finished_at=now(), progress='complete',
                            result_refs={'run_id': run['run_id']})
        except Exception as exc:
            _, error = describe(exc, self.jobs[jid]['request_id'], job=True)
            blocked = error['code'] in {'MODEL_UNAVAILABLE', 'CALIBRATION_REQUIRED', 'STALE_REVISION', 'WRITER_BUSY'}
            self.update_job(jid, state='BLOCKED' if blocked else 'FAILED', finished_at=now(),
                            progress='stopped', error=error)

    def job(self, workspace_id, job_id):
        token(workspace_id)
        with self.guard:
            require(workspace_id in self.workspaces, 'WORKSPACE_NOT_FOUND', 'Unknown workspace')
            job = self.jobs.get(job_id)
            require(job is not None and job['workspace_id'] == workspace_id, 'JOB_NOT_FOUND', 'Unknown job')
            return copy.deepcopy(job)

    def diagnostics(self, workspace_id):
        ws = self.workspace(workspace_id)
        with self.guard:
            jobs = [j for j in self.jobs.values() if j['workspace_id'] == workspace_id]
            latest = jobs[-1]['state'] if jobs else None
        with ws.read() as c:
            snapshot = c.execute('SELECT 1 FROM snapshot LIMIT 1').fetchone() is not None
        acceptance_warnings = (['provider=synthetic_acceptance', 'mode=test/acceptance',
                                'SYNTHETIC_ONLY_NO_REAL_MODEL_VALIDATION']
                               if isinstance(self.provider, SyntheticAcceptanceProvider) else [])
        return {'workspace_initialized': True, 'schema_version': core.CONTRACT,
                'model_availability': 'CONFIGURED' if self.provider is not None else 'MODEL_UNAVAILABLE',
                'calibration_status': 'PUBLIC_CALIBRATION_GAP', 'latest_run_state': latest,
                'snapshot_available': snapshot,
                'warnings': ['PUBLIC_CALIBRATION_GAP', 'LICENSE_UNDECIDED', 'AURAFACE_PROVENANCE_UNRESOLVED',
                             'REAL_MODEL_SMOKE_SKIPPED', 'IN_PROCESS_JOBS_NOT_DURABLE'] + acceptance_warnings}
