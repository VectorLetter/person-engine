"""Public synthetic launch and domain regression; anonymous disposable inputs only."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from person_engine.api import create_app
from person_engine.api.__main__ import main
from person_engine.api.synthetic_acceptance import SyntheticAcceptanceProvider
from person_engine.core_io import FoundationError
from api_fixture import ApiFlow, decision


class SyntheticLaunchTests(unittest.TestCase):
    def launch(self, args):
        with patch('sys.argv', ['person-engine-api', *args]), \
             patch('person_engine.api.__main__.create_app') as app, \
             patch('person_engine.api.__main__.uvicorn.run') as run:
            main()
            return app.call_args, run.call_args

    def test_explicit_opt_in(self):
        app, run = self.launch(['--synthetic-acceptance', '--root', 'disposable-root'])
        self.assertIsInstance(app.kwargs['provider'], SyntheticAcceptanceProvider)
        self.assertEqual(run.kwargs['host'], '127.0.0.1')
        self.assertEqual(run.kwargs['workers'], 1)

    def test_default_does_not_fallback(self):
        with patch.dict('os.environ', {'PERSON_ENGINE_PROVIDER': 'synthetic_acceptance'}):
            app, _ = self.launch([])
        self.assertIsNone(app.kwargs['provider'])
        self.assertEqual(app.args, (Path('workspaces'),))

    def test_synthetic_requires_explicit_root(self):
        with self.assertRaises(SystemExit) as error:
            self.launch(['--synthetic-acceptance'])
        self.assertEqual(error.exception.code, 2)

    def test_synthetic_and_real_options_conflict(self):
        with self.assertRaises(SystemExit) as error:
            self.launch(['--synthetic-acceptance', '--root', 'disposable-root', '--model-config', 'unused.json'])
        self.assertEqual(error.exception.code, 2)

    def test_bad_model_config_never_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td)/'missing.json')
            with self.assertRaises(SystemExit) as error:
                self.launch(['--model-config', missing])
            self.assertEqual(error.exception.code, 2)


class SyntheticInferenceTests(unittest.TestCase):
    def test_deterministic_detection(self):
        for value in (32, 64, 160, 192):
            image=np.full((96,96,3),value,dtype=np.uint8)
            first=SyntheticAcceptanceProvider().detect(image)
            second=SyntheticAcceptanceProvider().detect(image.copy())
            self.assertEqual(len(first),1)
            np.testing.assert_array_equal(first,second)
            self.assertEqual(first[0][:4].tolist(),[2.,2.,30.,30.])

    def test_deterministic_embeddings_and_manifest(self):
        for value,vector in [(32,(1.,0.,0.)),(64,(1.,0.,0.)),(160,(0.,1.,0.)),(192,(0.,0.,1.))]:
            image=np.full((96,96,3),value,dtype=np.uint8)
            for _ in range(2):
                provider=SyntheticAcceptanceProvider()
                self.assertEqual(provider.embed(image,provider.detect(image)[0]),vector)
                self.assertEqual(provider.manifest['calibration_status'],'SYNTHETIC_ONLY')
        self.assertEqual(SyntheticAcceptanceProvider().manifest,SyntheticAcceptanceProvider().manifest)

    def test_no_face_fixture(self):
        provider=SyntheticAcceptanceProvider();image=np.full((96,96,3),224,dtype=np.uint8)
        self.assertEqual(provider.detect(image),[])
        with self.assertRaises(FoundationError) as error:
            provider.embed(image,None)
        self.assertEqual(error.exception.code,'EMBEDDING_INVALID')

    def test_rejects_nonfixtures(self):
        images=[np.full((96,96,3),100,dtype=np.uint8),np.full((32,32,3),32,dtype=np.uint8),
                np.full((96,96,3),32,dtype=np.float64)]
        changed=np.full((96,96,3),32,dtype=np.uint8);changed[20,20,0]=33;images.append(changed)
        for image in images:
            with self.assertRaises(FoundationError) as error:
                SyntheticAcceptanceProvider().detect(image)
            self.assertEqual(error.exception.code,'DETECTION_INVALID')


class SyntheticHttpTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='anonymous_acceptance_');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()/'service'
        self.client=self.enterContext(TestClient(create_app(self.root,provider=SyntheticAcceptanceProvider()),base_url='http://127.0.0.1'))
        self.flow=ApiFlow(self.client,self.root);self.base=self.flow.base

    def seed(self):
        self.assets={k:self.flow.asset(k,v)[0]['asset_id'] for k,v in [('A',32),('B',64),('C',160),('D',224),('E',192)]}
        self.review=self.flow.review(list(self.assets.values()))
        candidates=self.review['source']['candidates']
        self.by_label={k:next(c for c in candidates if c['asset_id']==aid) for k,aid in self.assets.items() if k!='D'}
        return self.review

    def save(self):
        self.seed()
        ds=[decision(self.by_label[k],display_name='Synthetic A',person_key='synthetic-a',enroll_reference=True) for k in ('A','B')]
        ds += [decision(self.by_label['C'],'OUTSIDER'),decision(self.by_label['E'],'IGNORE_FACE')]
        self.decisions=ds
        return self.flow.save(self.review,ds,key='save-acceptance')

    def test_empty_roster_grouping_and_repeat_run(self):
        review=self.seed();source=review['source']
        self.assertEqual(source['roster_person_ids'],[])
        self.assertEqual(len(source['candidates']),4)
        self.assertTrue(all(c['state']=='UNKNOWN' and c['person_id'] is None and c['candidate_person_id'] is None for c in source['candidates']))
        groups={frozenset(g['face_ids']) for g in source['groups']}
        self.assertEqual(groups,{frozenset(self.by_label[k]['face_id'] for k in ('A','B')),frozenset([self.by_label['C']['face_id']]),frozenset([self.by_label['E']['face_id']])})
        other=self.flow.review([])
        self.assertEqual(other['source'],source)

    def test_valid_decision_duplicate_and_stale(self):
        saved=self.save()
        self.assertEqual(saved['revision'],1)
        self.assertEqual(self.flow.save(self.review,self.decisions,key='save-acceptance'),saved)
        stale=self.flow.save(self.review,self.decisions,key='stale-save',status=409)
        self.assertEqual(stale['code'],'STALE_REVISION')
        self.assertEqual(self.flow.get(self.base+'/people')['items'],[])

    def test_apply_replay_no_duplicate_facts(self):
        saved=self.save();applied=self.flow.apply(saved,key='apply-acceptance');basis=self.flow.basis()
        for key in ('apply-acceptance','apply-other-key'):
            r=self.client.post(self.base+'/reviews/'+saved['session_id']+'/apply',json={'revision':saved['revision']},headers={'Idempotency-Key':key})
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.headers['X-Apply-Outcome'],'IDEMPOTENT_REPLAY')
            self.assertEqual(r.json()['data'],applied)
            self.assertEqual(self.flow.basis(),basis)
        self.assertEqual(len(self.flow.get(self.base+'/people')['items']),1)
        self.assertEqual(len(self.flow.get(self.base+'/assignments')['items']),2)

    def test_snapshot_reflects_applied_facts(self):
        applied=self.flow.apply(self.save());basis=self.flow.basis()
        pub=self.flow.post(self.base+'/snapshots',{'expected_basis':basis},'publish-acceptance')
        response=self.client.get(self.base+'/snapshots/'+pub['snapshot_id']+'/export')
        self.assertEqual(response.status_code,200);doc=response.json()
        self.assertEqual(hashlib.sha256(response.content).hexdigest(),pub['payload_sha256'])
        self.assertEqual(doc,self.flow.get(self.base+'/snapshots/'+pub['snapshot_id']))
        self.assertEqual((len(doc['persons']),len(doc['faces']),len(doc['assignments'])),(1,4,2))
        self.assertEqual({f['state'] for f in doc['faces']},{'ASSIGNED','OUTSIDER','IGNORED'})
        self.assertEqual(sum(x['ignored'] for x in doc['ignored_state']),1)
        self.assertEqual(len(doc['provenance']['assets']),5)
        self.assertEqual(len(doc['provenance']['inference']),5)
        self.assertEqual(doc['provenance']['basis'],basis)
        self.assertTrue(all(x['manifest']['calibration_status']=='SYNTHETIC_ONLY' for x in doc['provenance']['inference']))
        self.assertEqual(doc['compatibility']['calibration_status'],'PUBLIC CALIBRATION GAP')
        self.assertTrue(applied['event_ids'])

    def test_diagnostics_mark_synthetic_without_calibration_claim(self):
        d=self.flow.get(self.base+'/diagnostics')
        self.assertEqual(d['model_availability'],'CONFIGURED')
        self.assertEqual(d['calibration_status'],'PUBLIC_CALIBRATION_GAP')
        self.assertTrue({'provider=synthetic_acceptance','mode=test/acceptance','PUBLIC_CALIBRATION_GAP','REAL_MODEL_SMOKE_SKIPPED'}<=set(d['warnings']))

    def test_default_service_nonempty_ingest_stays_blocked(self):
        with TestClient(create_app(self.root.parent/'default-service'),base_url='http://127.0.0.1') as client:
            flow=ApiFlow(client,self.root.parent/'default-service')
            asset,_=flow.asset(value=32);job,_=flow.run([asset['asset_id']])
            self.assertEqual(job['state'],'BLOCKED');self.assertEqual(job['error']['code'],'MODEL_UNAVAILABLE')
            self.assertNotIn('provider=synthetic_acceptance',flow.get(flow.base+'/diagnostics')['warnings'])
