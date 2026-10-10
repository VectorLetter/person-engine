"""Anonymous synthetic-only test fixture. Never imported by runtime modules."""
import hashlib
from pathlib import Path
import cv2
import numpy as np

from person_engine.public_v0 import CONTRACT, initialize, register, ingest, match, create_session, save_decisions, apply, publish_snapshot, read_snapshot, export_snapshot

# Deliberately synthetic test values. Not shipped runtime defaults or calibration.
POLICY = {'match_similarity': 0.8, 'match_margin': 0.1, 'cluster_similarity': 0.8}


class SyntheticProvider:
    manifest = {'kind': 'synthetic-fixture', 'detector_sha256': 'a' * 64, 'embedding_sha256': 'b' * 64,
                'embedding_model': 'anonymous-test-vectors', 'embedding_dim': 3,
                'detector_config': {'confidence': 0.5, 'nms': 0.3, 'top_k': 10},
                'preprocess_version': 'synthetic-only-v1', 'calibration_status': 'SYNTHETIC_ONLY'}

    def __init__(self, vectors=((1., 0., 0.),)):
        self.vectors = vectors
        self.detect_calls = 0
        self.embed_calls = 0

    def detect(self, image):
        self.detect_calls += 1
        assert image.shape[:2] == (96, 96)  # Actual image decode, not a bypass.
        return [np.array([i * 36 + 2, 2, 30, 30, 8, 10, 25, 10, 16, 17, 10, 25, 23, 25, .9]) for i in range(len(self.vectors))]

    def embed(self, image, row):
        self.embed_calls += 1
        return self.vectors[int((row[0] - 2) / 36)]


def picture(root, name='anonymous.png', value=100):
    path = Path(root) / name
    cv2.imwrite(str(path), np.full((96, 96, 3), value, dtype=np.uint8))
    return path


def request(ws, image, ref, key, **changes):
    raw = image.read_bytes()
    r = {'contract': CONTRACT, 'workspace_id': ws.workspace_id, 'request_id': key,
         'source_namespace': 'anonymous-fixture', 'source_ref': ref,
         'content_sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
         'media_type': 'image/png', 'expected_revision': ws.basis()['revision']}
    r.update(changes)
    return r


def add(ws, image, ref, provider=None):
    r = register(ws, request(ws, image, ref, 'reg-' + ref), image)
    return ingest(ws, r['asset_id'], provider or SyntheticProvider(), 'ingest-' + ref, ws.basis())


def session(ws, key='review'):
    run = match(ws, 'match-' + key, ws.basis(), POLICY)
    return create_session(ws, run['run_id'], key)


def decision(candidate, action, **kw):
    return {'face_id': candidate['face_id'], 'action': action,
            'expected_state': {k: candidate[k] for k in ('state', 'person_id')}, **kw}


def decide(ws, s, ds, key='save', apply_key='apply'):
    saved = save_decisions(ws, s['session_id'], s['revision'], ds, key)
    return apply(ws, s['session_id'], saved['revision'], apply_key)


def end_to_end(root):
    root = Path(root)
    ws = initialize(root / 'fresh-workspace')
    image = picture(root)
    provider = SyntheticProvider()
    first = add(ws, image, 'source-a', provider)
    other = picture(root, 'second.png', 120)
    add(ws, other, 'source-b')
    run = match(ws, 'first-run', ws.basis(), POLICY)
    assert run['roster_person_ids'] == [] and len(run['groups']) == 1
    assert all(x['state'] == 'UNKNOWN' and x['candidate_person_id'] is None for x in run['candidates'])
    s = create_session(ws, run['run_id'], 'first-review')
    applied = decide(ws, s, [decision(x, 'NEW_PERSON', display_name='Anonymous A', person_key='a', enroll_reference=True)
                             for x in s['source']['candidates']])
    assert len(applied['new_persons']) == 1
    third = picture(root, 'third.png', 150)
    add(ws, third, 'source-c')
    one = match(ws, 'one-person', ws.basis(), POLICY)
    fresh = next(x for x in one['candidates'] if x['state'] == 'UNKNOWN')
    assert fresh['candidate_person_id'] == applied['new_persons']['a']
    assert fresh['comparison'] == 'SINGLE_REFERENCE_PERSON_NO_MARGIN_EVIDENCE'
    s2 = create_session(ws, one['run_id'], 'second-review')
    decide(ws, s2, [decision(fresh, 'EXISTING_PERSON', person_id=applied['new_persons']['a'])], 'save2', 'apply2')
    frozen = ws.basis()
    pub = publish_snapshot(ws, 'publication', frozen)
    assert publish_snapshot(ws, 'publication', frozen) == pub
    export = export_snapshot(ws, pub['snapshot_id'])
    import json
    consumer = json.loads((ws.home / export['relative_path']).read_text())
    assert consumer == read_snapshot(ws, pub['snapshot_id'])
    assert len(consumer['persons']) == 1 and len(consumer['assignments']) == 3
    assert provider.detect_calls == 1 and provider.embed_calls == 1
    assert first['manifest']['kind'] == 'synthetic-fixture'
    return {'status': 'PASS', 'fixture': 'PUBLIC_V0_SYNTHETIC_E2E',
            'steps': ['init', 'register', 'decode', 'detect', 'embed', 'empty-roster-cluster', 'review',
                      'apply', 'single-person-match', 'confirm', 'publish', 'export', 'consumer-readback'],
            'persons': 1, 'faces': 3, 'assignments': 3, 'snapshot_version': pub['snapshot_version'],
            'real_model_quality_validated': False, 'calibration_status': 'PUBLIC CALIBRATION GAP'}
