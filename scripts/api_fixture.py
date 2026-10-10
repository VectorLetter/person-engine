"""Anonymous fixtures: all domain operations enter through HTTP."""
import hashlib
import time
from public_v0_fixture import POLICY, picture


class ApiFlow:
    def __init__(self, client, root):
        self.client, self.root = client, root
        self.counter = 0
        self.workspace = self.post('/v1/workspaces', {'workspace_key': 'anonymous'})
        self.wid = self.workspace['workspace_id']
        self.base = '/v1/workspaces/' + self.wid

    def post(self, path, body, key=None, status=None):
        self.counter += 1
        response = self.client.post(path, json=body, headers={'Idempotency-Key': key or f'request-{self.counter}'}) if path != '/v1/workspaces' else self.client.post(path, json=body)
        assert response.status_code == status if status else response.is_success, response.text
        return response.json()['data'] if response.is_success else response.json()

    def get(self, path):
        response = self.client.get(path)
        assert response.status_code == 200, response.text
        return response.json()['data']

    def basis(self):
        return self.get(self.base)['basis']

    def asset(self, name='anonymous', value=100):
        image = picture(self.root / self.workspace['source_directory'], name + '.png', value)
        data = image.read_bytes()
        body = {'source_namespace': 'anonymous-fixture', 'source_ref': name, 'source_path': image.name,
                'content_sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                'media_type': 'image/png', 'expected_revision': self.basis()['revision']}
        return self.post(self.base + '/assets', body), body

    def run(self, assets, policy=POLICY, key=None):
        body = {'asset_ids': assets, 'expected_basis': self.basis(), 'policy': policy}
        job = self.post(self.base + '/ingest-runs', body, key=key)
        return self.poll(job['job_id']), body

    def poll(self, jid):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = self.get(self.base + '/jobs/' + jid)
            if job['state'] in {'SUCCEEDED', 'FAILED', 'BLOCKED'}:
                return job
            time.sleep(.01)
        raise AssertionError('Job polling deadline exceeded')

    def review(self, assets):
        job, _ = self.run(assets)
        assert job['state'] == 'SUCCEEDED', job
        return self.post(self.base + '/reviews', {'run_id': job['result_refs']['run_id']})

    def save(self, review, decisions, key=None, status=None):
        return self.post(self.base + '/reviews/' + review['session_id'] + '/decisions',
                         {'revision': review['revision'], 'decisions': decisions}, key, status)

    def apply(self, saved, key=None, status=None):
        return self.post(self.base + '/reviews/' + saved['session_id'] + '/apply',
                         {'revision': saved['revision']}, key, status)


def decision(candidate, action='NEW_PERSON', **kwargs):
    return {'face_id': candidate['face_id'], 'action': action,
            'expected_state': {k: candidate[k] for k in ('state', 'person_id')}, **kwargs}


def end_to_end(client, root):
    flow = ApiFlow(client, root)
    assets = [flow.asset(name, value)[0]['asset_id'] for name, value in [('one', 100), ('two', 120)]]
    review = flow.review(assets)
    for endpoint in ('people', 'faces', 'assignments'):
        flow.get(flow.base + '/' + endpoint)
    saved = flow.save(review, [decision(c, display_name='Anonymous A', person_key='a', enroll_reference=True)
                               for c in review['source']['candidates']])
    applied = flow.apply(saved)
    assert flow.apply(saved) == applied
    people = flow.get(flow.base + '/people')['items']
    assert len(people) == 1
    assert flow.get(flow.base + '/people/' + people[0]['person_id']) == people[0]
    assert len(flow.get(flow.base + '/assignments')['items']) == 2
    pub = flow.post(flow.base + '/snapshots', {'expected_basis': flow.basis()}, 'publish')
    response = client.get(flow.base + '/snapshots/' + pub['snapshot_id'] + '/export')
    assert response.status_code == 200
    consumer = response.json()
    assert consumer == flow.get(flow.base + '/snapshots/' + pub['snapshot_id'])
    assert hashlib.sha256(response.content).hexdigest() == pub['payload_sha256']
    assert len(consumer['assignments']) == 2 and len(consumer['persons']) == 1
    assert 'embedding' not in consumer['faces'][0]
    return {'name': 'PUBLIC_V0_API_SYNTHETIC_E2E', 'status': 'PASS', 'persons': 1, 'faces': 2,
            'assignments': 2, 'snapshot_version': pub['snapshot_version'],
            'steps': ['workspace', 'register', 'ingest-run', 'poll', 'read', 'review', 'decisions', 'apply', 'snapshot', 'export', 'readback'],
            'calibration_status': 'PUBLIC_CALIBRATION_GAP', 'real_model_smoke': 'SKIP'}
