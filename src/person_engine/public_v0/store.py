"""Fresh-only V0 authority. All domain mutations share one SQLite transaction."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import uuid

from ..core_io import FoundationError, canonical, digest, now, atomic_json
from ..writer_lock import WriterLock

CONTRACT = 'person-engine-public-v0/1'
PRODUCER = 'person-engine-public-v0/0.1.0'
SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE asset(asset_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source_ref TEXT NOT NULL,
 content_sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
 registration_id TEXT NOT NULL, UNIQUE(namespace,source_ref));
CREATE TABLE receipt(kind TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(kind,request_id));
CREATE TABLE person(person_id TEXT PRIMARY KEY, display_name TEXT NOT NULL UNIQUE, status TEXT NOT NULL);
CREATE TABLE inference(asset_id TEXT NOT NULL REFERENCES asset, model_space TEXT NOT NULL,
 manifest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(asset_id,model_space));
CREATE TABLE face(face_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES asset,
 detection_id TEXT NOT NULL, model_space TEXT NOT NULL, row_index INTEGER NOT NULL,
 geometry TEXT NOT NULL, embedding TEXT NOT NULL,
 UNIQUE(asset_id,model_space,row_index));
CREATE TABLE human_state(face_id TEXT PRIMARY KEY REFERENCES face, state TEXT NOT NULL
 CHECK(state IN ('ASSIGNED','OUTSIDER','IGNORED','UNASSIGNED')), person_id TEXT REFERENCES person,
 event_id TEXT NOT NULL, CHECK((state='ASSIGNED' AND person_id IS NOT NULL) OR
 (state!='ASSIGNED' AND person_id IS NULL)));
CREATE TABLE rejection(face_id TEXT REFERENCES face, person_id TEXT REFERENCES person,
 event_id TEXT NOT NULL, PRIMARY KEY(face_id,person_id));
CREATE TABLE reference(face_id TEXT PRIMARY KEY REFERENCES face, person_id TEXT NOT NULL REFERENCES person,
 model_space TEXT NOT NULL, event_id TEXT NOT NULL);
CREATE TABLE machine_result(run_id TEXT PRIMARY KEY, basis TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE session(session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
 basis TEXT NOT NULL, source TEXT NOT NULL, decisions TEXT NOT NULL, applied_receipt TEXT);
CREATE TABLE review_revision(session_id TEXT REFERENCES session, revision INTEGER,
 decisions TEXT NOT NULL, PRIMARY KEY(session_id,revision));
CREATE TABLE event(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE snapshot(snapshot_id TEXT PRIMARY KEY, version INTEGER NOT NULL UNIQUE,
 document TEXT NOT NULL, receipt TEXT NOT NULL);
'''
for table in ('receipt', 'inference', 'face', 'asset', 'machine_result', 'review_revision', 'event', 'snapshot'):
    for action in ('UPDATE', 'DELETE'):
        SCHEMA += f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable {table}'); END;\n"


def require(ok, code, message):
    if not ok:
        raise FoundationError(code, message)


def token(value, field='identifier'):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value),
            'INVALID_REQUEST', f'Invalid {field}')
    return value


def exact_keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            'INVALID_REQUEST', 'Missing or unsupported fields')


def hook(fault, point, connection=None):
    if fault:
        fault(point, connection)


def safe_home(value):
    p = Path(value).expanduser().absolute()
    require(not any(x.is_symlink() for x in (p, *p.parents)), 'UNSAFE_PATH', 'Symlink workspace')
    return p.resolve()


def initialize(home):
    home = safe_home(home)
    require(not home.exists() or home.is_dir() and not any(home.iterdir()),
            'WORKSPACE_EXISTS', 'A new or empty directory is required')
    home.mkdir(parents=True, exist_ok=True)
    for folder in ('state', 'content', 'exports'):
        (home / folder).mkdir()
    wid = 'ws_' + uuid.uuid4().hex
    c = sqlite3.connect(home / 'state/public_v0.sqlite')
    try:
        c.executescript(SCHEMA)
        c.executemany('INSERT INTO meta VALUES(?,?)', [('contract', CONTRACT), ('workspace_id', wid), ('revision', '0')])
        c.commit()
    finally:
        c.close()
    atomic_json(home / 'workspace.json', {'contract': CONTRACT, 'workspace_id': wid})
    return Workspace(home)


class Workspace:
    def __init__(self, home):
        self.home = safe_home(home)
        self.db = self.home / 'state/public_v0.sqlite'
        for p in (self.home / 'workspace.json', self.db, self.home / 'state', self.home / 'content', self.home / 'exports'):
            require(p.exists() and not p.is_symlink(), 'INVALID_WORKSPACE', 'Missing or unsafe V0 resource')
        self.info = json.loads((self.home / 'workspace.json').read_text())
        require(self.info.get('contract') == CONTRACT, 'INCOMPATIBLE_WORKSPACE', 'Not a fresh V0 workspace')
        self.workspace_id = token(self.info.get('workspace_id'), 'workspace_id')
        with self.read() as c:
            require(meta(c, 'contract') == CONTRACT and meta(c, 'workspace_id') == self.workspace_id,
                    'INCOMPATIBLE_WORKSPACE', 'Workspace identity mismatch')

    def _connect(self, readonly=False):
        require(not self.db.is_symlink() and not self.db.parent.is_symlink(), 'UNSAFE_PATH', 'Unsafe database')
        c = sqlite3.connect(self.db.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=0)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        if readonly:
            c.execute('PRAGMA query_only=ON')
        return c

    @contextmanager
    def read(self):
        # SQLite hot-journal recovery is local to this fresh V0 DB. Acquire the
        # same kernel writer lease; never replay external/private recovery data.
        if Path(str(self.db) + '-journal').exists():
            with self.write() as recovery:
                recovery.execute('SELECT value FROM meta LIMIT 1').fetchone()
        c = self._connect(True)
        try:
            c.execute('BEGIN')
            yield c
        finally:
            c.rollback()
            c.close()

    @contextmanager
    def write(self):
        with WriterLock(self.home, 'public-v0 transaction'):
            c = self._connect()
            try:
                c.execute('BEGIN IMMEDIATE')
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise
            finally:
                c.close()

    def basis(self):
        with self.read() as c:
            return basis(c)

    def inspect(self):
        with self.read() as c:
            return {'workspace_id': self.workspace_id, 'basis': basis(c),
                    'persons': rows(c, 'person'), 'faces': public_faces(c),
                    'assets': rows(c, 'asset'), 'calibration_status': 'PUBLIC CALIBRATION GAP',
                    'identity_authority': 'human-only'}


def rows(c, table):
    # All callers pass internal table names, never request strings.
    return sorted((dict(r) for r in c.execute(f'SELECT * FROM {table}')), key=canonical)


def meta(c, key):
    return c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]


def bump(c):
    value = int(meta(c, 'revision')) + 1
    c.execute("UPDATE meta SET value=? WHERE key='revision'", (str(value),))
    return value


def basis(c):
    source = {table: rows(c, table) for table in
              ('asset', 'face', 'inference', 'person', 'human_state', 'rejection', 'reference', 'event')}
    return {'workspace_id': meta(c, 'workspace_id'), 'revision': int(meta(c, 'revision')),
            'source_digest': digest(source)}


def check_basis(c, expected):
    require(expected == basis(c), 'STALE_SOURCE', 'Source facts or registry changed')


def replay(c, kind, request_id, payload):
    token(request_id, 'request_id')
    row = c.execute('SELECT * FROM receipt WHERE kind=? AND request_id=?', (kind, request_id)).fetchone()
    if row:
        require(row['request_digest'] == digest(payload), 'CONFLICT', 'Request key has different semantics')
        return json.loads(row['result'])
    return None


def record(c, kind, request_id, payload, result):
    c.execute('INSERT INTO receipt VALUES(?,?,?,?)', (kind, request_id, digest(payload), canonical(result)))
    return result


def state(c, face_id):
    row = c.execute('SELECT state,person_id FROM human_state WHERE face_id=?', (face_id,)).fetchone()
    return dict(row) if row else {'state': 'UNKNOWN', 'person_id': None}


def public_faces(c):
    return [{'face_id': r['face_id'], 'asset_id': r['asset_id'], 'detection_id': r['detection_id'],
             'model_space': r['model_space'], 'row_index': r['row_index'], 'geometry': json.loads(r['geometry']),
             **state(c, r['face_id'])} for r in c.execute('SELECT * FROM face ORDER BY face_id')]
