"""HTTP adapter regression and boundary tests, using fresh anonymous workspaces."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from person_engine.api import create_app
from person_engine import public_v0 as core
from person_engine.core_io import FoundationError
from public_v0_fixture import SyntheticProvider, POLICY, picture
from api_fixture import ApiFlow, decision, end_to_end


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='anonymous_api_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'service'
        self.provider = SyntheticProvider()
        self.app = create_app(self.root, provider=self.provider)
        self.client = self.enterContext(TestClient(self.app, base_url='http://127.0.0.1', raise_server_exceptions=False))
        self.flow = ApiFlow(self.client, self.root)
        self.base = self.flow.base

    def seeded(self):
        asset, _ = self.flow.asset()
        review = self.flow.review([asset['asset_id']])
        return review, review['source']['candidates'][0]

    def assigned(self):
        review, candidate = self.seeded()
        saved = self.flow.save(review, [decision(candidate, display_name='Anonymous A', person_key='a')])
        return self.flow.apply(saved), saved

    def error(self, response, code, status):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body['code'], code)
        self.assertEqual(set(body), {'code', 'message', 'request_id', 'details'})
        self.assertEqual(body['request_id'], response.headers['x-request-id'])
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn('Traceback', response.text)

    def test_launcher_localhost_default(self):
        from person_engine.api.__main__ import main
        with patch('sys.argv', ['person-engine-api']), patch('person_engine.api.__main__.uvicorn.run') as run:
            main()
        self.assertEqual(run.call_args.kwargs['host'], '127.0.0.1')
        self.assertEqual(run.call_args.kwargs['port'], 8000)
        self.assertEqual(run.call_args.kwargs['workers'], 1)
        self.assertFalse(run.call_args.kwargs['access_log'])
        self.assertFalse(run.call_args.kwargs['proxy_headers'])

    def test_validation_does_not_echo_input(self):
        response = self.client.post('/v1/workspaces', json={'workspace_key': '/private/sensitive-input'})
        self.error(response, 'INVALID_REQUEST', 422)
        self.assertNotIn('sensitive-input', response.text)
        self.error(self.client.get('/health', headers={'X-Request-ID': 'bad/id'}), 'INVALID_REQUEST', 422)

    def test_health(self):
        self.assertEqual(self.client.get('/health').json(), {'status': 'ok'})

    def test_openapi(self):
        schema = self.client.get('/openapi.json').json()
        self.assertEqual(len(schema['paths']), 19)
        self.assertIn('DecisionSave', schema['components']['schemas'])
        self.assertEqual(self.client.get('/docs').status_code, 200)
        for path, methods in schema['paths'].items():
            for method, operation in methods.items():
                self.assertIn('schema', next(iter(operation['responses'].values()))['content']['application/json'])

    def test_workspace_create_read_conflict(self):
        read = self.flow.get(self.base)
        self.assertEqual(read['workspace_id'], self.flow.wid)
        self.assertEqual(read['basis']['revision'], 0)
        r = self.client.post('/v1/workspaces', json={'workspace_key': 'anonymous'}, headers={'Idempotency-Key': 'anonymous'})
        self.error(r, 'WORKSPACE_EXISTS', 409)
        self.assertEqual(self.flow.get(self.base), read)

    def test_workspace_isolation(self):
        self.flow.asset()
        other = self.flow.post('/v1/workspaces', {'workspace_key': 'other'})
        self.assertEqual(self.flow.get('/v1/workspaces/' + other['workspace_id'])['basis']['revision'], 0)
        self.error(self.client.get('/v1/workspaces/ws_missing'), 'WORKSPACE_NOT_FOUND', 404)

    def test_register_replay(self):
        asset, body = self.flow.asset()
        replay = self.flow.post(self.base + '/assets', body, 'request-2', status=200)
        self.assertTrue(replay['replay'])
        self.assertEqual(replay['receipt'], asset['receipt'])
        body['expected_revision'] = self.flow.basis()['revision']
        bound = self.flow.post(self.base + '/assets', body, 'new-key', status=200)
        self.assertEqual(bound['receipt']['action'], 'BOUND_REPLAY')

    def test_replay_after_source_removed(self):
        asset, body = self.flow.asset()
        (self.root / self.flow.workspace['source_directory'] / body['source_path']).unlink()
        replay = self.flow.post(self.base + '/assets', body, 'request-2', status=200)
        self.assertEqual(replay['receipt'], asset['receipt'])
        body.update(source_ref='new-ref', expected_revision=self.flow.basis()['revision'])
        self.error(self.client.post(self.base + '/assets', json=body, headers={'Idempotency-Key': 'new-source'}), 'SOURCE_CHANGED', 409)

    def test_registration_conflict(self):
        _, body = self.flow.asset()
        body['source_ref'] = 'different'
        self.error(self.client.post(self.base + '/assets', json=body, headers={'Idempotency-Key': 'request-2'}), 'ASSET_CONFLICT', 409)

    def test_registration_binding_conflict(self):
        _, body = self.flow.asset()
        body.update(content_sha256='c' * 64, expected_revision=self.flow.basis()['revision'])
        self.error(self.client.post(self.base + '/assets', json=body, headers={'Idempotency-Key': 'new'}), 'ASSET_CONFLICT', 409)

    def test_invalid_source_paths(self):
        _, body = self.flow.asset()
        for path in ('../workspace.json', '/etc/passwd', '../../other/imports/a.png', './anonymous.png', 'a\\b'):
            with self.subTest(path=path):
                body['source_path'] = path
                self.error(self.client.post(self.base + '/assets', json=body, headers={'Idempotency-Key': 'unsafe'}), 'UNSAFE_PATH', 422)

    def test_symlink_and_hardlink_source(self):
        _, body = self.flow.asset()
        imports = self.root / self.flow.workspace['source_directory']
        (imports / 'link.png').symlink_to(imports / 'anonymous.png')
        (imports / 'hard.png').hardlink_to(imports / 'anonymous.png')
        for name in ('link.png', 'hard.png', 'anonymous.png'):
            body['source_path'] = name
            self.error(self.client.post(self.base + '/assets', json=body, headers={'Idempotency-Key': 'unsafe'}), 'UNSAFE_PATH', 422)

    def test_symlink_workspace_component(self):
        ws = self.app.state.service.workspace(self.flow.wid)
        content = ws.home / 'content'
        content.rmdir()
        content.symlink_to(ws.home / 'imports', target_is_directory=True)
        self.error(self.client.get(self.base), 'INVALID_WORKSPACE', 422)

    def test_job_success_replay_conflict(self):
        asset, _ = self.flow.asset()
        job, body = self.flow.run([asset['asset_id']], key='job')
        self.assertEqual(job['state'], 'SUCCEEDED')
        self.assertEqual(self.provider.detect_calls, 1)
        replay = self.flow.post(self.base + '/ingest-runs', body, 'job', status=200)
        self.assertEqual(replay, job)
        body['asset_ids'] = []
        self.error(self.client.post(self.base + '/ingest-runs', json=body, headers={'Idempotency-Key': 'job'}), 'CONFLICT', 409)

    def test_job_failure_no_traceback(self):
        asset, _ = self.flow.asset()
        with patch.object(self.provider, 'detect', side_effect=RuntimeError('private-path-and-vector-secret')):
            job, _ = self.flow.run([asset['asset_id']])
        self.assertEqual(job['state'], 'FAILED')
        self.assertEqual(job['error']['code'], 'RUN_FAILED')
        self.assertNotIn('private-path', json.dumps(job))
        self.assertEqual(self.flow.get(self.base + '/faces')['total'], 0)

    def test_job_model_unavailable(self):
        asset, _ = self.flow.asset()
        self.app.state.service.provider = None
        job, _ = self.flow.run([asset['asset_id']])
        self.assertEqual((job['state'], job['error']['code']), ('BLOCKED', 'MODEL_UNAVAILABLE'))

    def test_job_calibration_required(self):
        job, _ = self.flow.run([], policy=None)
        self.assertEqual((job['state'], job['error']['code']), ('BLOCKED', 'CALIBRATION_REQUIRED'))

    def test_job_not_found(self):
        self.error(self.client.get(self.base + '/jobs/job_missing'), 'JOB_NOT_FOUND', 404)

    def test_job_queued_running_nonblocking(self):
        asset, _ = self.flow.asset()
        entered, release = threading.Event(), threading.Event()
        real = self.provider.detect
        def slow(image):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('timeout')
            return real(image)
        try:
            with patch.object(self.provider, 'detect', side_effect=slow):
                first = self.flow.post(self.base + '/ingest-runs', {'asset_ids': [asset['asset_id']], 'expected_basis': self.flow.basis(), 'policy': POLICY})
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.flow.get(self.base + '/jobs/' + first['job_id'])['state'], 'RUNNING')
                second = self.flow.post(self.base + '/ingest-runs', {'asset_ids': [], 'expected_basis': self.flow.basis(), 'policy': POLICY})
                self.assertEqual(self.flow.get(self.base + '/jobs/' + second['job_id'])['state'], 'QUEUED')
                self.assertEqual(self.client.get('/health').status_code, 200)
                release.set()
                self.assertEqual(self.flow.poll(first['job_id'])['state'], 'SUCCEEDED')
                self.assertEqual(self.flow.poll(second['job_id'])['state'], 'BLOCKED')
        finally:
            release.set()

    def test_people_faces_assignments(self):
        applied, _ = self.assigned()
        person_id = applied['new_persons']['a']
        self.assertEqual(self.flow.get(self.base + '/people/' + person_id)['display_name'], 'Anonymous A')
        self.assertEqual(self.flow.get(self.base + '/people')['total'], 1)
        faces = self.flow.get(self.base + '/faces')
        self.assertEqual(faces['total'], 1)
        self.assertNotIn('embedding', json.dumps(faces))
        self.assertEqual(self.flow.get(self.base + '/assignments')['items'][0]['person_id'], person_id)
        self.assertEqual(self.flow.get(self.base + '/faces?offset=1&limit=1')['items'], [])

    def test_review_source_and_candidates(self):
        review, _ = self.seeded()
        read = self.flow.get(self.base + '/reviews/' + review['session_id'])
        self.assertEqual(read['basis'], self.flow.basis())
        self.assertEqual(read['source']['basis'], read['basis'])
        self.assertEqual(self.flow.get(self.base + '/reviews/' + review['session_id'] + '/candidates')['total'], 1)

    def test_decision_duplicate_and_conflict(self):
        review, candidate = self.seeded()
        ds = [decision(candidate, 'IGNORE_FACE')]
        first = self.flow.save(review, ds, 'save')
        self.assertEqual(self.flow.save(review, ds, 'save'), first)
        changed = [decision(candidate, 'OUTSIDER')]
        self.assertEqual(self.flow.save(review, changed, 'save', 409)['code'], 'CONFLICT')

    def test_stale_review_revision(self):
        review, candidate = self.seeded()
        self.flow.save(review, [decision(candidate, 'IGNORE_FACE')])
        self.assertEqual(self.flow.save(review, [decision(candidate, 'IGNORE_FACE')], status=409)['code'], 'STALE_REVISION')

    def test_stale_domain_apply(self):
        review, candidate = self.seeded()
        saved = self.flow.save(review, [decision(candidate, 'IGNORE_FACE')])
        self.flow.asset('advance', 120)
        self.assertEqual(self.flow.apply(saved, status=409)['code'], 'STALE_REVISION')

    def test_ignore_face(self):
        review, candidate = self.seeded()
        self.flow.apply(self.flow.save(review, [decision(candidate, 'IGNORE_FACE')]))
        self.assertEqual(self.flow.get(self.base + '/faces')['items'][0]['state'], 'IGNORED')
        self.assertEqual(self.flow.get(self.base + '/assignments')['total'], 0)

    def test_correction(self):
        self.assigned()
        review = self.flow.review([])
        candidate = review['source']['candidates'][0]
        self.flow.apply(self.flow.save(review, [decision(candidate, 'CORRECTION', to={'state': 'OUTSIDER', 'person_id': None})]))
        self.assertEqual(self.flow.get(self.base + '/faces')['items'][0]['state'], 'OUTSIDER')
        self.assertEqual(self.flow.get(self.base + '/assignments')['total'], 0)

    def test_reject_unassign_existing_person(self):
        applied, _ = self.assigned()
        person_id = applied['new_persons']['a']
        for action in ('UNASSIGN', 'EXISTING_PERSON', 'REJECT'):
            review = self.flow.review([])
            candidate = review['source']['candidates'][0]
            extra = {'person_id': person_id} if action != 'UNASSIGN' else {}
            self.flow.apply(self.flow.save(review, [decision(candidate, action, **extra)]))
        self.assertEqual(self.flow.get(self.base + '/faces')['items'][0]['state'], 'UNASSIGNED')

    def test_invalid_decision_no_mutation(self):
        review, candidate = self.seeded()
        basis = self.flow.basis()
        bad = decision(candidate, 'CORRECTION', to={'state': 'UNKNOWN', 'person_id': None})
        self.assertEqual(self.flow.save(review, [bad], status=422)['code'], 'INVALID_DECISION')
        self.assertEqual(self.flow.basis(), basis)

    def test_atomic_apply_rollback(self):
        review, candidate = self.seeded()
        saved = self.flow.save(review, [decision(candidate, display_name='Anonymous A', person_key='a')])
        basis = self.flow.basis()
        real = core.apply
        def fail(ws, sid, revision, key):
            def fault(point, connection):
                if point == 'after_human_event':
                    raise RuntimeError('private internal traceback payload')
            return real(ws, sid, revision, key, fault=fault)
        with patch('person_engine.api.core.apply', side_effect=fail):
            self.assertEqual(self.flow.apply(saved, 'apply', 500)['code'], 'INTERNAL_ERROR')
        self.assertEqual(self.flow.basis(), basis)
        self.assertEqual(self.flow.get(self.base + '/people')['total'], 0)
        self.assertEqual(self.flow.get(self.base + '/reviews/' + saved['session_id'])['status'], 'DRAFT')
        self.flow.apply(saved, 'apply')

    def test_duplicate_apply(self):
        applied, saved = self.assigned()
        basis = self.flow.basis()
        self.assertEqual(self.flow.apply(saved, 'another-key'), applied)
        self.assertEqual(self.flow.basis(), basis)

    def test_snapshot_publish_replay_conflict_export(self):
        self.assigned()
        body = {'expected_basis': self.flow.basis()}
        first = self.flow.post(self.base + '/snapshots', body, 'publish')
        self.assertEqual(self.flow.post(self.base + '/snapshots', body, 'publish'), first)
        self.assertEqual(self.flow.get(self.base + '/snapshots')['total'], 1)
        exported = self.client.get(self.base + '/snapshots/' + first['snapshot_id'] + '/export')
        self.assertEqual(hashlib.sha256(exported.content).hexdigest(), first['payload_sha256'])
        self.assertEqual(exported.json(), self.flow.get(self.base + '/snapshots/' + first['snapshot_id']))
        self.flow.asset('advance', 120)
        changed = {'expected_basis': self.flow.basis()}
        self.assertEqual(self.flow.post(self.base + '/snapshots', changed, 'publish', 409)['code'], 'CONFLICT')
        self.assertEqual(self.flow.post(self.base + '/snapshots', body, 'new-publish', 409)['code'], 'STALE_REVISION')

    def test_diagnostics(self):
        data = self.flow.get(self.base + '/diagnostics')
        self.assertEqual(data['calibration_status'], 'PUBLIC_CALIBRATION_GAP')
        self.assertIn('LICENSE_UNDECIDED', data['warnings'])
        self.assertNotIn(str(self.root), json.dumps(data))

    def test_error_schema_request_id(self):
        r = self.client.get('/unknown', headers={'X-Request-ID': 'correlation-test'})
        self.error(r, 'NOT_FOUND', 404)
        self.assertEqual(r.json()['request_id'], 'correlation-test')
        self.error(self.client.post('/v1/workspaces', json={'workspace_key': '../escape'}), 'INVALID_REQUEST', 422)

    def test_no_traceback_leakage(self):
        with patch('person_engine.api.service.Service.workspace_view', side_effect=RuntimeError('private exception marker')):
            response = self.client.get(self.base)
        self.error(response, 'INTERNAL_ERROR', 500)
        self.assertNotIn('private exception marker', response.text)

    def test_cross_workspace_access(self):
        applied, _ = self.assigned()
        review = self.flow.review([])
        job, _ = self.flow.run([])
        snapshot = self.flow.post(self.base + '/snapshots', {'expected_basis': self.flow.basis()})
        other = self.flow.post('/v1/workspaces', {'workspace_key': 'other'})
        base = '/v1/workspaces/' + other['workspace_id']
        for suffix in ['/reviews/' + review['session_id'], '/jobs/' + job['job_id'],
                       '/people/' + applied['new_persons']['a'], '/snapshots/' + snapshot['snapshot_id']]:
            self.assertEqual(self.client.get(base + suffix).status_code, 404)
        response = self.client.post(base + '/ingest-runs', json={'asset_ids': [], 'expected_basis': self.flow.basis(), 'policy': POLICY}, headers={'Idempotency-Key': 'cross'})
        self.error(response, 'WORKSPACE_MISMATCH', 409)
        foreign_asset = self.flow.get(self.base + '/faces')['items'][0]['asset_id']
        response = self.client.post(base + '/ingest-runs', json={'asset_ids': [foreign_asset], 'expected_basis': other['basis'], 'policy': POLICY}, headers={'Idempotency-Key': 'cross'})
        self.error(response, 'NOT_FOUND', 404)

    def test_localhost_and_origin_boundary(self):
        self.error(self.client.get('/health', headers={'Host': 'remote.example'}), 'LOCAL_ONLY', 403)
        self.error(self.client.post('/v1/workspaces', json={'workspace_key': 'web'}, headers={'Origin': 'https://remote.example'}), 'LOCAL_ONLY', 403)

    def test_body_limit(self):
        self.error(self.client.post('/v1/workspaces', content=b'x' * (1024 * 1024 + 1), headers={'Content-Type': 'application/json'}), 'REQUEST_TOO_LARGE', 413)

    def test_missing_idempotency_key(self):
        self.error(self.client.post(self.base + '/snapshots', json={'expected_basis': self.flow.basis()}), 'INVALID_REQUEST', 422)

    def test_single_process_lease(self):
        from person_engine.api.service import Service
        with self.assertRaises(FoundationError) as raised:
            Service(self.root)
        self.assertEqual(raised.exception.code, 'WRITER_BUSY')

    def test_root_rejects_existing_workspace(self):
        from person_engine.api.service import Service
        private = Path(self.temp.name) / 'unrelated'
        private.mkdir()
        (private / 'sentinel').write_text('unchanged')
        with self.assertRaises(FoundationError):
            Service(private)
        self.assertEqual(list(private.iterdir()), [private / 'sentinel'])

    def test_restart_preserves_core_not_jobs(self):
        with tempfile.TemporaryDirectory(prefix='anonymous_restart_') as td:
            root = Path(td).resolve() / 'root'
            with TestClient(create_app(root, provider=SyntheticProvider()), base_url='http://127.0.0.1') as client:
                flow = ApiFlow(client, root)
                job, _ = flow.run([])
                original = flow.get(flow.base)
            with TestClient(create_app(root), base_url='http://127.0.0.1') as client:
                self.assertEqual(client.get(flow.base).json()['data'], original)
                self.assertEqual(client.get(flow.base + '/jobs/' + job['job_id']).status_code, 404)

    def test_public_v0_api_synthetic_e2e(self):
        with tempfile.TemporaryDirectory(prefix='anonymous_api_e2e_') as td:
            root = Path(td).resolve() / 'root'
            with TestClient(create_app(root, provider=SyntheticProvider()), base_url='http://127.0.0.1') as client:
                self.assertEqual(end_to_end(client, root)['status'], 'PASS')

    def test_contract_openapi(self):
        schema = self.client.get('/openapi.json').json()
        ids = []
        for path, methods in schema['paths'].items():
            for method, operation in methods.items():
                ids.append(operation['operationId'])
                self.assertTrue(operation.get('description'), (method, path))
                headers = {h['name'].lower(): h for h in operation.get('parameters', []) if h['in'] == 'header'}
                if method == 'post' and path != '/v1/workspaces':
                    self.assertTrue(headers['idempotency-key']['required'])
                if method == 'get':
                    self.assertNotIn('idempotency-key', headers)
                self.assertIn('405', operation['responses'])
        self.assertEqual(len(ids), len(set(ids)))
        fields = schema['components']['schemas']['NewDecision']['properties']
        self.assertEqual(fields['enroll_reference']['type'], 'boolean')

    def test_decision_explicit_null_rejected_by_schema(self):
        from person_engine.api.models import NewDecision
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            NewDecision(face_id='face_test', expected_state={'state': 'UNKNOWN', 'person_id': None},
                        action='NEW_PERSON', display_name='Anonymous A', person_key='a', enroll_reference=None)

    def test_apply_outcome_header(self):
        review, candidate = self.seeded()
        saved = self.flow.save(review, [decision(candidate, 'IGNORE_FACE')])
        path = self.base + '/reviews/' + saved['session_id'] + '/apply'
        results = []
        for key, outcome in [('once', 'SUCCESS'), ('once', 'IDEMPOTENT_REPLAY'), ('again', 'IDEMPOTENT_REPLAY')]:
            response = self.client.post(path, json={'revision': saved['revision']}, headers={'Idempotency-Key': key})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get('X-Apply-Outcome'), outcome)
            results.append(response.json()['data'])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    def test_restart_same_key_does_not_resurrect_old_job(self):
        with tempfile.TemporaryDirectory(prefix='anonymous_restart_contract_') as td:
            root = Path(td).resolve() / 'root'
            with TestClient(create_app(root), base_url='http://127.0.0.1') as client:
                flow = ApiFlow(client, root)
                old, body = flow.run([], key='same-key')
            with TestClient(create_app(root), base_url='http://127.0.0.1') as client:
                new = client.post(flow.base + '/ingest-runs', json=body, headers={'Idempotency-Key': 'same-key'})
                self.assertEqual(new.status_code, 202)
                self.assertNotEqual(new.json()['data']['job_id'], old['job_id'])
                response = client.get(flow.base + '/jobs/' + old['job_id'])
                self.assertEqual((response.status_code, response.json()['code']), (404, 'JOB_NOT_FOUND'))

    def test_unknown_exception_code_is_not_exposed(self):
        with patch('person_engine.api.service.Service.workspace_view', side_effect=FoundationError('untrusted-code-marker', 'untrusted-message-marker')):
            response = self.client.get(self.base)
        self.error(response, 'INTERNAL_ERROR', 500)
        self.assertNotIn('untrusted', response.text)

    def test_equal_content_different_logical_assets(self):
        first, _ = self.flow.asset('first', 100)
        second, _ = self.flow.asset('second', 100)
        self.assertEqual(first['receipt']['content_sha256'], second['receipt']['content_sha256'])
        self.assertNotEqual(first['asset_id'], second['asset_id'])
        self.assertEqual((first['status'], second['status']), ('CREATED', 'CREATED'))

    def test_method_error_contract(self):
        self.error(self.client.delete('/health'), 'HTTP_ERROR', 405)


if __name__ == '__main__':
    unittest.main(verbosity=2)
