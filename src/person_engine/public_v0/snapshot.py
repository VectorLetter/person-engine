"""Immutable authoritative JSON publication, then independently atomic export."""
import hashlib
import json

from ..core_io import canonical, digest, now
from .store import PRODUCER, require, replay, record, check_basis, rows, public_faces, hook
from .registration import install_bytes

SNAPSHOT_SCHEMA = 'person-facts/v0.1'


def publish_snapshot(ws, request_id, expected_basis, *, fault=None):
    payload = {'basis': expected_basis, 'schema': SNAPSHOT_SCHEMA}
    with ws.write() as c:
        previous = replay(c, 'publish', request_id, payload)
        if previous is not None:
            return previous
        check_basis(c, expected_basis)
        version = c.execute('SELECT coalesce(max(version),0)+1 FROM snapshot').fetchone()[0]
        sid = 'snapshot_' + digest([ws.workspace_id, request_id, payload])
        faces = public_faces(c)
        doc = {'workspace_id': ws.workspace_id, 'snapshot_id': sid, 'snapshot_version': version,
               'schema_version': SNAPSHOT_SCHEMA, 'producer_version': PRODUCER,
               'persons': rows(c, 'person'), 'faces': faces,
               'assignments': [{'face_id': r['face_id'], 'person_id': r['person_id'], 'event_id': r['event_id']}
                               for r in c.execute("SELECT * FROM human_state WHERE state='ASSIGNED' ORDER BY face_id")],
               'ignored_state': [{'face_id': r['face_id'], 'ignored': r['state'] == 'IGNORED', 'state': r['state'], 'event_id': r['event_id']}
                                 for r in c.execute('SELECT * FROM human_state ORDER BY face_id')],
               'provenance': {'assets': rows(c, 'asset'),
                              'inference': [json.loads(r[0]) for r in c.execute('SELECT result FROM inference ORDER BY asset_id,model_space')],
                              'human_events': [json.loads(r[0]) for r in c.execute('SELECT payload FROM event ORDER BY event_id')],
                              'rejections': rows(c, 'rejection'),
                              'basis': expected_basis},
               'source_digest': expected_basis['source_digest'], 'generated_at': now(),
               'compatibility': {'contract_family': 'person-facts', 'mode': 'FULL_SNAPSHOT',
                                 'domain_revision': expected_basis['revision'], 'minimum_reader_schema': SNAPSHOT_SCHEMA,
                                 'identity_authority': 'human-only', 'calibration_status': 'PUBLIC CALIBRATION GAP'}}
        validate_snapshot(doc)
        hook(fault, 'after_snapshot_build', c)
        check_basis(c, expected_basis)
        encoded = canonical(doc) + '\n'
        receipt = {'workspace_id': ws.workspace_id, 'snapshot_id': sid, 'snapshot_version': version,
                   'schema_version': SNAPSHOT_SCHEMA, 'source_digest': doc['source_digest'],
                   'content_digest': digest(doc), 'payload_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
                   'published_at': doc['generated_at']}
        c.execute('INSERT INTO snapshot VALUES(?,?,?,?)', (sid, version, encoded, canonical(receipt)))
        hook(fault, 'before_snapshot_commit', c)
        return record(c, 'publish', request_id, payload, receipt)


def validate_snapshot(doc):
    require(doc['schema_version'] == SNAPSHOT_SCHEMA, 'SNAPSHOT_INVALID', 'Unsupported snapshot')
    people = {p['person_id'] for p in doc['persons']}
    faces = {f['face_id']: f for f in doc['faces']}
    assigned = set()
    for a in doc['assignments']:
        require(a['face_id'] in faces and a['person_id'] in people and a['face_id'] not in assigned,
                'SNAPSHOT_INVALID', 'Invalid assignment reference')
        require(faces[a['face_id']]['state'] == 'ASSIGNED' and faces[a['face_id']]['person_id'] == a['person_id'],
                'SNAPSHOT_INVALID', 'Inconsistent human projection')
        assigned.add(a['face_id'])
    require(assigned == {f['face_id'] for f in doc['faces'] if f['state'] == 'ASSIGNED'},
            'SNAPSHOT_INVALID', 'Missing assignment')
    require(not any(r['ignored'] and r['face_id'] in assigned for r in doc['ignored_state']),
            'SNAPSHOT_INVALID', 'Ignored face has assignment')


def read_snapshot(ws, snapshot_id):
    with ws.read() as c:
        row = c.execute('SELECT * FROM snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone()
        require(row is not None, 'NOT_FOUND', 'Snapshot is not published')
        doc, receipt = json.loads(row['document']), json.loads(row['receipt'])
        require(digest(doc) == receipt['content_digest'] and
                hashlib.sha256(row['document'].encode()).hexdigest() == receipt['payload_sha256'],
                'SNAPSHOT_INVALID', 'Published bytes changed')
        validate_snapshot(doc)
        return doc


def export_snapshot(ws, snapshot_id, *, fault=None):
    doc = read_snapshot(ws, snapshot_id)
    target = ws.home / 'exports' / (snapshot_id + '.json')
    data = (canonical(doc) + '\n').encode()
    install_bytes(target, data, fault=fault)
    return {'snapshot_id': snapshot_id, 'relative_path': 'exports/' + target.name,
            'payload_sha256': hashlib.sha256(data).hexdigest()}
