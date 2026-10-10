"""Inference and provisional grouping; never writes person or human facts."""
import json
import math
import tempfile

import numpy as np

from ..core_io import canonical, digest, file_sha
from ..ingest_cache import decode
from .providers import validate_manifest
from .store import require, replay, record, check_basis, basis, bump, hook, public_faces, state

MAX_FACES_PER_ASSET = 256
MAX_MATCH_FACES = 512  # Explicit bounded V0 batch; not a recognition threshold.


def ingest(ws, asset_id, provider, request_id, expected_basis, *, fault=None):
    manifest = json.loads(canonical(provider.manifest))
    validate_manifest(manifest)
    model_space = digest(manifest)
    payload = {'asset_id': asset_id, 'model': manifest, 'basis': expected_basis}
    with ws.write() as c:
        prior = replay(c, 'ingest', request_id, payload)
        if prior is not None:
            return prior
        check_basis(c, expected_basis)
        a = c.execute('SELECT * FROM asset WHERE asset_id=?', (asset_id,)).fetchone()
        require(a is not None, 'NOT_FOUND', 'Unknown asset')
        spaces = {r[0] for r in c.execute('SELECT DISTINCT model_space FROM inference')}
        require(not spaces or spaces == {model_space}, 'MODEL_SPACE_CONFLICT', 'V0 workspace pins one compute/model space')
        old = c.execute('SELECT result FROM inference WHERE asset_id=? AND model_space=?', (asset_id, model_space)).fetchone()
        if old:
            return record(c, 'ingest', request_id, payload, json.loads(old[0]))
        path = ws.home / 'content' / a['content_sha256']
        require(not path.is_symlink() and path.is_file() and file_sha(path) == a['content_sha256'],
                'SOURCE_CHANGED', 'Registered bytes changed')
        # Existing bounded macOS JPEG/PNG decoder; provider is injected, never loaded from a string.
        with tempfile.TemporaryDirectory(prefix='pe_v0_decode_') as td:
            image = decode(path, td)
        detections = list(provider.detect(image))
        require(len(detections) <= MAX_FACES_PER_ASSET, 'CONTENT_LIMIT', 'Too many detections')
        faces = []
        height, width = image.shape[:2]
        for i, raw in enumerate(detections):
            row = np.asarray(raw, dtype=np.float64)
            require(row.shape == (15,) and np.isfinite(row).all() and row[2] > 0 and row[3] > 0
                    and row[0] >= 0 and row[1] >= 0 and row[0] + row[2] <= width + 1
                    and row[1] + row[3] <= height + 1 and 0 <= row[14] <= 1,
                    'DETECTION_INVALID', 'Invalid detection geometry/score')
            vector = np.asarray(provider.embed(image, row), dtype=np.float64)
            require(vector.shape == (manifest['embedding_dim'],) and np.isfinite(vector).all()
                    and np.isfinite(np.linalg.norm(vector)) and np.linalg.norm(vector) > 0, 'EMBEDDING_INVALID', 'Invalid embedding')
            vector = vector / np.linalg.norm(vector)
            did = 'detection_' + digest([a['content_sha256'], model_space, i, row.tolist()])
            fid = 'face_' + digest([ws.workspace_id, asset_id, did])
            geometry = {'bbox': row[:4].tolist(), 'landmarks': row[4:14].reshape(5, 2).tolist(), 'score': float(row[14])}
            c.execute('INSERT INTO face VALUES(?,?,?,?,?,?,?)',
                      (fid, asset_id, did, model_space, i, canonical(geometry), canonical(vector.tolist())))
            faces.append(fid)
            hook(fault, 'after_face_insert', c)
        require(provider.manifest == manifest and file_sha(path) == a['content_sha256'],
                'SOURCE_CHANGED', 'Provider contract or source changed during inference')
        if hasattr(provider, 'verify'):
            provider.verify()
        revision = bump(c)
        result = {'asset_id': asset_id, 'face_ids': faces, 'model_space': model_space,
                  'manifest': manifest, 'revision': revision, 'status': 'INGESTED' if faces else 'NO_FACES'}
        c.execute('INSERT INTO inference VALUES(?,?,?,?)', (asset_id, model_space, canonical(manifest), canonical(result)))
        return record(c, 'ingest', request_id, payload, result)


def validate_policy(policy):
    require(isinstance(policy, dict) and set(policy) == {'match_similarity', 'match_margin', 'cluster_similarity'},
            'CONFIG_REQUIRED', 'All three candidate policy parameters must be explicit')
    require(all(type(x) in (int, float) and math.isfinite(x) for x in policy.values()),
            'CONFIG_REQUIRED', 'Finite numeric policy required')
    require(-1 <= policy['match_similarity'] <= 1 and -1 <= policy['cluster_similarity'] <= 1
            and 0 <= policy['match_margin'] <= 2, 'CONFIG_REQUIRED', 'Invalid cosine policy range')


def group_faces(faces, vectors, similarity):
    """Bounded complete-link agglomeration with component-level cannot-link.

    No calibration constants or private benchmark material. IDs derive from members
    in match(), not random UUIDs. Two different detections from the same content
    cannot merge, including when those bytes have multiple logical asset owners.
    """
    if not faces:
        return []
    sims = np.asarray(vectors) @ np.asarray(vectors).T
    parents = list(range(len(faces)))
    groups = {i: {i} for i in parents}

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    edges = sorted(((-float(sims[i, j]), i, j) for i in range(len(faces)) for j in range(i + 1, len(faces))
                    if sims[i, j] >= similarity))
    for _, i, j in edges:
        a, b = root(i), root(j)
        if a == b:
            continue
        pairs = [(x, y) for x in groups[a] for y in groups[b]]
        if any((faces[x]['asset_id'] == faces[y]['asset_id'] or
                faces[x]['content_sha256'] == faces[y]['content_sha256'] and
                faces[x]['detection_id'] != faces[y]['detection_id']) or sims[x, y] < similarity for x, y in pairs):
            continue
        parents[b] = a
        groups[a] |= groups.pop(b)
    return [sorted(faces[i]['face_id'] for i in members) for _, members in sorted(groups.items())]


def match(ws, request_id, expected_basis, policy):
    validate_policy(policy)
    payload = {'basis': expected_basis, 'policy': policy}
    with ws.write() as c:
        prior = replay(c, 'match', request_id, payload)
        if prior is not None:
            return prior
        check_basis(c, expected_basis)
        faces = [dict(r) for r in c.execute('SELECT f.*,a.content_sha256 FROM face f JOIN asset a USING(asset_id) ORDER BY face_id')]
        require(len(faces) <= MAX_MATCH_FACES, 'CONTENT_LIMIT', 'V0 matching batch exceeds supported face limit')
        spaces = {f['model_space'] for f in faces}
        require(len(spaces) <= 1, 'MODEL_SPACE_CONFLICT', 'Mixed compute spaces')
        reference_rows = list(c.execute('SELECT r.*,f.embedding,f.detection_id FROM reference r JOIN face f USING(face_id) '
                                       "JOIN human_state h USING(face_id) JOIN person p ON p.person_id=r.person_id "
                                       "WHERE h.state='ASSIGNED' AND h.person_id=r.person_id AND p.status='active' ORDER BY r.face_id"))
        refs, seen = {}, set()
        for r in reference_rows:
            if spaces and r['model_space'] not in spaces:
                continue
            key = (r['person_id'], r['detection_id'])
            if key not in seen:
                refs.setdefault(r['person_id'], []).append(np.asarray(json.loads(r['embedding'])))
                seen.add(key)
        all_vectors = {f['face_id']: np.asarray(json.loads(f['embedding'])) for f in faces}
        available, vectors, candidates = [], [], []
        by_public = {f['face_id']: f for f in public_faces(c)}
        for f in faces:
            current = state(c, f['face_id'])
            rejected = {r[0] for r in c.execute('SELECT person_id FROM rejection WHERE face_id=?', (f['face_id'],))}
            scores = sorted([{'person_id': p, 'similarity': float(max(np.dot(all_vectors[f['face_id']], e) for e in v))}
                             for p, v in refs.items() if p not in rejected], key=lambda s: (-s['similarity'], s['person_id']))
            proposed = None
            # Zero roster -> unknown; one roster -> absolute check only and explicitly no competitor evidence.
            if current['state'] == 'UNKNOWN' and scores and scores[0]['similarity'] >= policy['match_similarity']:
                if len(scores) == 1 or scores[0]['similarity'] - scores[1]['similarity'] >= policy['match_margin']:
                    proposed = scores[0]['person_id']
            candidates.append({**by_public[f['face_id']], 'candidate_person_id': proposed,
                               'scores': scores, 'comparison': 'NO_REFERENCES' if not scores else
                               'SINGLE_REFERENCE_PERSON_NO_MARGIN_EVIDENCE' if len(scores) == 1 else 'MULTI_PERSON',
                               'authority': 'NON_AUTHORITATIVE', 'rejected_person_ids': sorted(rejected)})
            if current['state'] == 'UNKNOWN':
                available.append(f); vectors.append(all_vectors[f['face_id']])
        run_id = 'run_' + digest([ws.workspace_id, payload])
        groups = [{'cluster_id': 'cluster_' + digest([run_id, members]), 'face_ids': members,
                   'kind': 'PROVISIONAL_UNKNOWN_GROUP', 'person_id': None}
                  for members in group_faces(available, vectors, policy['cluster_similarity'])]
        result = {'run_id': run_id, 'basis': expected_basis, 'policy': policy, 'candidates': candidates,
                  'groups': groups, 'roster_person_ids': sorted(refs), 'calibration_status': 'PUBLIC CALIBRATION GAP',
                  'identity_authority': 'human-only', 'model_spaces': sorted(spaces)}
        result['result_digest'] = digest(result)
        existing = c.execute('SELECT result FROM machine_result WHERE run_id=?', (run_id,)).fetchone()
        if existing:
            require(json.loads(existing[0]) == result, 'CONFLICT', 'Run identity collision')
        else:
            c.execute('INSERT INTO machine_result VALUES(?,?,?)', (run_id, canonical(expected_basis), canonical(result)))
        return record(c, 'match', request_id, payload, result)
