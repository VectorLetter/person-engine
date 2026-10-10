"""Fresh disposable V0 tests; no production discovery or data access."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from person_engine.core_io import FoundationError, digest, canonical
from person_engine.public_v0 import *
from person_engine.public_v0.runtime import group_faces, MAX_MATCH_FACES
from person_engine.public_v0.store import bump
from public_v0_fixture import SyntheticProvider, POLICY, picture, request, add, session, decision, decide, end_to_end


class V0Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public_v0_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = initialize(self.root / 'workspace')
        self.image = picture(self.root)

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(FoundationError) as e:
            fn(*args, **kwargs)
        self.assertEqual(e.exception.code, code)

    def count(self, table):
        with self.ws.read() as c:
            return c.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def seed(self):
        add(self.ws, self.image, 'a')
        return session(self.ws)

    def human(self):
        s = self.seed()
        r = decide(self.ws, s, [decision(s['source']['candidates'][0], 'NEW_PERSON', display_name='Anonymous A', person_key='a', enroll_reference=True)])
        return r['new_persons']['a']

    def fail(self, point):
        def run(actual, connection):
            if actual == point:
                raise RuntimeError('synthetic fault')
        return run


class RegistrationTests(V0Case):
    def test_fresh_registration_and_receipt(self):
        r = register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image)
        self.assertEqual(r['action'], 'REGISTERED'); self.assertEqual(self.count('asset'), 1)
        self.assertEqual(r['receipt_digest'], digest({k:v for k,v in r.items() if k != 'receipt_digest'}))

    def test_exact_request_replay_without_source(self):
        q = request(self.ws, self.image, 'a', 'r'); first = register(self.ws, q, self.image)
        self.image.unlink()
        self.assertEqual(first, register(self.ws, q, self.image))
        self.assertEqual(self.count('asset'), 1)

    def test_new_request_same_binding_same_identity(self):
        a = register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image)
        b = register(self.ws, request(self.ws, self.image, 'a', 'r2'), self.image)
        self.assertEqual(a['asset_id'], b['asset_id']); self.assertEqual(b['action'], 'BOUND_REPLAY')
        self.assertEqual(self.count('asset'), 1)

    def test_same_bytes_different_source_different_asset(self):
        a = register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image)
        b = register(self.ws, request(self.ws, self.image, 'b', 'r2'), self.image)
        self.assertNotEqual(a['asset_id'], b['asset_id']); self.assertEqual(self.count('asset'), 2)
        self.assertEqual(len(list((self.ws.home/'content').iterdir())), 1)

    def test_request_key_conflict(self):
        q = request(self.ws, self.image, 'a', 'r'); register(self.ws, q, self.image)
        self.error('CONFLICT', register, self.ws, dict(q, source_ref='b'), self.image)

    def test_binding_content_conflict(self):
        register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image)
        other = picture(self.root, 'other.png', 200)
        self.error('CONFLICT', register, self.ws, request(self.ws, other, 'a', 'r2'), other)

    def test_stale_revision(self):
        q = request(self.ws, self.image, 'b', 'r2')
        register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image)
        self.error('STALE_SOURCE', register, self.ws, q, self.image)

    def test_incompatible_contract(self):
        self.error('INCOMPATIBLE_REQUEST', register, self.ws, request(self.ws, self.image, 'a', 'r', contract='legacy'), self.image)

    def test_workspace_isolation(self):
        other = initialize(self.root/'other'); q = request(self.ws, self.image, 'a', 'r')
        self.error('WORKSPACE_MISMATCH', register, other, q, self.image)
        a = register(self.ws, q, self.image); b = register(other, request(other, self.image, 'a', 'r'), self.image)
        self.assertNotEqual(a['asset_id'], b['asset_id'])

    def test_receipt_deterministic_replay(self):
        q = request(self.ws, self.image, 'a', 'r'); a = register(self.ws, q, self.image)
        self.assertEqual(canonical(a), canonical(register(self.ws, q, self.image)))

    def test_registration_failure_rolls_back_and_retries(self):
        q = request(self.ws, self.image, 'a', 'r'); before = self.ws.basis()
        with self.assertRaises(RuntimeError): register(self.ws, q, self.image, fault=self.fail('after_asset_insert'))
        self.assertEqual(before, self.ws.basis()); self.assertEqual(self.count('receipt'), 0)
        register(self.ws, q, self.image); self.assertEqual(self.count('asset'), 1)

    def test_changed_source(self):
        q = request(self.ws, self.image, 'a', 'r'); self.image.write_bytes(b'invalid')
        self.error('SOURCE_CHANGED', register, self.ws, q, self.image)

    def test_ref_path_rejected(self):
        self.error('INVALID_REQUEST', register, self.ws, request(self.ws, self.image, '/absolute/private', 'r'), self.image)

    def test_workspace_no_overwrite(self):
        self.error('WORKSPACE_EXISTS', initialize, self.ws.home)
        (self.root/'link').symlink_to(self.ws.home)
        self.error('UNSAFE_PATH', Workspace, self.root/'link')


class RuntimeTests(V0Case):
    def test_decode_detect_embed_called(self):
        provider = SyntheticProvider(); r = add(self.ws, self.image, 'a', provider)
        self.assertEqual((provider.detect_calls, provider.embed_calls), (1, 1))
        self.assertEqual(len(r['face_ids']), 1)

    def test_ingest_replay_does_not_infer(self):
        r = register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image); p = SyntheticProvider(); basis = self.ws.basis()
        a = ingest(self.ws, r['asset_id'], p, 'i', basis)
        self.assertEqual(a, ingest(self.ws, r['asset_id'], p, 'i', basis)); self.assertEqual(p.detect_calls, 1)
        self.assertEqual(a, ingest(self.ws, r['asset_id'], p, 'i2', self.ws.basis())); self.assertEqual(p.detect_calls, 1)

    def test_no_faces_and_empty_result(self):
        r = add(self.ws, self.image, 'a', SyntheticProvider(()))
        self.assertEqual(r['status'], 'NO_FACES')
        out = match(self.ws, 'm', self.ws.basis(), POLICY)
        self.assertEqual(out['candidates'], []); self.assertEqual(out['groups'], [])

    def test_bad_image_decode_no_partial_faces(self):
        self.image.write_bytes(b'\x89PNG\r\n\x1a\nBAD')
        r = register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image)
        with self.assertRaises((ValueError, FoundationError)):
            ingest(self.ws, r['asset_id'], SyntheticProvider(), 'i', self.ws.basis())
        self.assertEqual(self.count('face'), 0); self.assertEqual(self.count('inference'), 0)

    def test_invalid_embedding_rollback(self):
        r = register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image); b = self.ws.basis()
        self.error('EMBEDDING_INVALID', ingest, self.ws, r['asset_id'], SyntheticProvider(((float('nan'), 0, 0),)), 'i', b)
        self.assertEqual(self.count('face'), 0); self.assertEqual(self.ws.basis(), b)

    def test_inference_midway_failure_rolls_back(self):
        r = register(self.ws, request(self.ws, self.image, 'a', 'r'), self.image); b = self.ws.basis()
        with self.assertRaises(RuntimeError):
            ingest(self.ws, r['asset_id'], SyntheticProvider(((1,0,0),(0,1,0))), 'i', b, fault=self.fail('after_face_insert'))
        self.assertEqual(self.ws.basis(), b); self.assertEqual(self.count('face'), 0)

    def test_model_space_change_blocked(self):
        add(self.ws, self.image, 'a')
        r = register(self.ws, request(self.ws, self.image, 'b', 'r2'), self.image)
        p = SyntheticProvider(); p.manifest = dict(p.manifest, embedding_sha256='c'*64)
        self.error('MODEL_SPACE_CONFLICT', ingest, self.ws, r['asset_id'], p, 'i', self.ws.basis())

    def test_empty_roster_unknown_not_outsider_or_person(self):
        self.seed(); out = match(self.ws, 'm', self.ws.basis(), POLICY)
        self.assertTrue(out['groups']); self.assertEqual(self.count('person'), 0)
        self.assertTrue(all(f['state']=='UNKNOWN' and f['candidate_person_id'] is None for f in out['candidates']))

    def test_first_100_photos_no_roster(self):
        for i in range(100): add(self.ws, self.image, f'a{i}')
        result = match(self.ws, 'hundred', self.ws.basis(), POLICY)
        self.assertEqual(len(result['candidates']), 100); self.assertEqual(self.count('person'), 0)
        self.assertEqual(sum(len(g['face_ids']) for g in result['groups']), 100)

    def test_single_person_candidate_remains_non_authoritative(self):
        pid = self.human(); add(self.ws, picture(self.root, 'other.png', 120), 'b')
        out = match(self.ws, 'm2', self.ws.basis(), POLICY)
        unknown = next(f for f in out['candidates'] if f['state']=='UNKNOWN')
        self.assertEqual(unknown['candidate_person_id'], pid)
        self.assertEqual(unknown['comparison'], 'SINGLE_REFERENCE_PERSON_NO_MARGIN_EVIDENCE')
        self.assertEqual(self.count('human_state'), 1)

    def test_multi_person_margin_and_tie(self):
        add(self.ws, self.image, 'a', SyntheticProvider(((1,0,0),(0,1,0))))
        s=session(self.ws)
        decide(self.ws,s,[decision(f,'NEW_PERSON',display_name='Anonymous '+str(i),person_key='p'+str(i),enroll_reference=True)
                          for i,f in enumerate(s['source']['candidates'])])
        add(self.ws,picture(self.root,'other.png',130),'b',SyntheticProvider(((1,1,0),)))
        p=dict(POLICY,match_similarity=0.6)
        result=match(self.ws,'two',self.ws.basis(),p)
        f=next(x for x in result['candidates'] if x['state']=='UNKNOWN')
        self.assertEqual(f['comparison'],'MULTI_PERSON');self.assertIsNone(f['candidate_person_id'])

    def test_same_content_separate_face_bindings(self):
        a=add(self.ws,self.image,'a');b=add(self.ws,self.image,'b')
        self.assertNotEqual(a['face_ids'],b['face_ids'])
        fs=self.ws.inspect()['faces'];self.assertEqual(fs[0]['detection_id'],fs[1]['detection_id'])

    def test_complete_link_and_component_cannot_link(self):
        faces=[{'face_id':str(i),'asset_id':a,'content_sha256':str(i),'detection_id':str(i)} for i,a in enumerate(['a','b','a'])]
        vectors=np.array([[1,0],[1,0],[1,0]])
        groups=group_faces(faces,vectors,.8)
        self.assertFalse(any('0' in x and '2' in x for x in groups))
        faces[2]['asset_id']='c'; vectors=np.array([[1,0],[.8,.6],[.28,.96]])
        groups=group_faces(faces,vectors,.7)
        self.assertFalse(any('0' in x and '2' in x for x in groups))

    def test_stable_cluster_ids_and_request_conflict(self):
        add(self.ws,self.image,'a');add(self.ws,self.image,'b');b=self.ws.basis()
        x=match(self.ws,'m',b,POLICY);y=match(self.ws,'m2',b,POLICY)
        self.assertEqual(x,y)
        self.error('CONFLICT',match,self.ws,'m',b,dict(POLICY,cluster_similarity=.9))

    def test_policy_required_no_default(self):
        self.error('CONFIG_REQUIRED',match,self.ws,'m',self.ws.basis(),{})
        self.error('CONFIG_REQUIRED',match,self.ws,'m',self.ws.basis(),dict(POLICY,match_margin=float('nan')))

    def test_no_human_overwrite_or_ignored_resurrection(self):
        s=self.seed();decide(self.ws,s,[decision(s['source']['candidates'][0],'IGNORE_FACE')]);b=self.ws.basis()
        result=match(self.ws,'again',b,POLICY)
        self.assertEqual(self.ws.basis(),b);self.assertEqual(result['candidates'][0]['state'],'IGNORED');self.assertEqual(result['groups'],[])


    def test_jpeg_decode_pipeline(self):
        image=picture(self.root,'anonymous.jpg',100)
        q=request(self.ws,image,'jpeg','r',media_type='image/jpeg')
        r=register(self.ws,q,image)
        result=ingest(self.ws,r['asset_id'],SyntheticProvider(),'i',self.ws.basis())
        self.assertEqual(len(result['face_ids']),1)


class ReviewTests(V0Case):
    def test_create_fetch_and_save_idempotency(self):
        s=self.seed();self.assertEqual(fetch_session(self.ws,s['session_id']),s)
        ds=[decision(s['source']['candidates'][0],'OUTSIDER')]
        r=save_decisions(self.ws,s['session_id'],0,ds,'s')
        self.assertEqual(r,save_decisions(self.ws,s['session_id'],0,ds,'s'))
        self.assertEqual(fetch_session(self.ws,s['session_id'])['revision'],1)

    def test_stale_draft_revision(self):
        s=self.seed();ds=[decision(s['source']['candidates'][0],'OUTSIDER')]
        save_decisions(self.ws,s['session_id'],0,ds,'s')
        self.error('STALE_REVISION',save_decisions,self.ws,s['session_id'],0,ds,'s2')

    def test_save_key_conflict(self):
        s=self.seed();f=s['source']['candidates'][0]
        save_decisions(self.ws,s['session_id'],0,[decision(f,'OUTSIDER')],'s')
        self.error('CONFLICT',save_decisions,self.ws,s['session_id'],0,[decision(f,'IGNORE_FACE')],'s')

    def test_apply_duplicate_same_and_new_key(self):
        s=self.seed();r=decide(self.ws,s,[decision(s['source']['candidates'][0],'OUTSIDER')]);b=self.ws.basis()
        self.assertEqual(r,apply(self.ws,s['session_id'],1,'apply'))
        self.assertEqual(r,apply(self.ws,s['session_id'],1,'apply2'))
        self.assertEqual(self.count('event'),1);self.assertEqual(self.ws.basis(),b)

    def test_stale_apply_revision(self):
        s=self.seed();save_decisions(self.ws,s['session_id'],0,[decision(s['source']['candidates'][0],'OUTSIDER')],'s')
        self.error('STALE_REVISION',apply,self.ws,s['session_id'],0,'a')

    def test_stale_source_after_other_mutation(self):
        s=self.seed();save_decisions(self.ws,s['session_id'],0,[decision(s['source']['candidates'][0],'OUTSIDER')],'s')
        register(self.ws,request(self.ws,self.image,'b','r2'),self.image)
        self.error('STALE_SOURCE',apply,self.ws,s['session_id'],1,'a');self.assertEqual(self.count('event'),0)

    def test_two_sessions_cannot_overwrite(self):
        s=self.seed();s2=session(self.ws,'second')
        save_decisions(self.ws,s2['session_id'],0,[decision(s2['source']['candidates'][0],'IGNORE_FACE')],'s2')
        decide(self.ws,s,[decision(s['source']['candidates'][0],'OUTSIDER')])
        self.error('STALE_SOURCE',apply,self.ws,s2['session_id'],1,'a2')
        self.assertEqual(self.ws.inspect()['faces'][0]['state'],'OUTSIDER')

    def test_apply_failure_rolls_back_new_person_references_events(self):
        s=self.seed();save_decisions(self.ws,s['session_id'],0,[decision(s['source']['candidates'][0],'NEW_PERSON',display_name='Anonymous A',person_key='a',enroll_reference=True)],'s')
        b=self.ws.basis()
        for point in ['after_human_event','before_apply_commit']:
            with self.assertRaises(RuntimeError): apply(self.ws,s['session_id'],1,'a',fault=self.fail(point))
            self.assertEqual(self.ws.basis(),b)
            for table in ['person','reference','event','human_state']:self.assertEqual(self.count(table),0)
            self.assertEqual(fetch_session(self.ws,s['session_id'])['status'],'DRAFT')
        apply(self.ws,s['session_id'],1,'a');self.assertEqual(self.count('person'),1)

    def test_multi_face_partial_failure_atomic(self):
        add(self.ws,self.image,'a',SyntheticProvider(((1,0,0),(0,1,0))));s=session(self.ws)
        save_decisions(self.ws,s['session_id'],0,[decision(f,'OUTSIDER') for f in s['source']['candidates']],'s')
        with self.assertRaises(RuntimeError):apply(self.ws,s['session_id'],1,'a',fault=self.fail('after_human_event'))
        self.assertEqual(self.count('human_state'),0);self.assertEqual(self.count('event'),0)

    def test_correction_removes_old_reference(self):
        pid=self.human();s=session(self.ws,'correction');f=s['source']['candidates'][0]
        r=decide(self.ws,s,[decision(f,'CORRECTION',to={'state':'UNASSIGNED','person_id':None})],'s2','a2')
        self.assertEqual(self.count('reference'),0);self.assertEqual(self.ws.inspect()['faces'][0]['state'],'UNASSIGNED')
        self.assertEqual(r,apply(self.ws,s['session_id'],1,'a2'));self.assertEqual(self.count('event'),2)

    def test_ignore_removes_assignment_and_reference(self):
        self.human();s=session(self.ws,'ignore')
        decide(self.ws,s,[decision(s['source']['candidates'][0],'IGNORE_FACE')],'s2','a2')
        self.assertEqual(self.ws.inspect()['faces'][0]['state'],'IGNORED');self.assertEqual(self.count('reference'),0)
        pub=publish_snapshot(self.ws,'p',self.ws.basis());doc=read_snapshot(self.ws,pub['snapshot_id'])
        self.assertEqual(doc['assignments'],[]);self.assertTrue(doc['ignored_state'][0]['ignored'])

    def test_outsider_is_not_ignored(self):
        s=self.seed();decide(self.ws,s,[decision(s['source']['candidates'][0],'OUTSIDER')])
        pub=publish_snapshot(self.ws,'p',self.ws.basis());doc=read_snapshot(self.ws,pub['snapshot_id'])
        self.assertEqual(doc['faces'][0]['state'],'OUTSIDER');self.assertFalse(doc['ignored_state'][0]['ignored'])

    def test_reject_and_explicit_reversal(self):
        pid=self.human();s=session(self.ws,'reject')
        decide(self.ws,s,[decision(s['source']['candidates'][0],'REJECT',person_id=pid)],'s2','a2')
        self.assertEqual(self.count('rejection'),1);self.assertEqual(self.count('reference'),0)
        s2=session(self.ws,'restore');f=s2['source']['candidates'][0]
        self.error('CORRECTION_REQUIRED',save_decisions,self.ws,s2['session_id'],0,[decision(f,'EXISTING_PERSON',person_id=pid)],'s3')
        decide(self.ws,s2,[decision(f,'CORRECTION',to={'state':'ASSIGNED','person_id':pid},enroll_reference=True)],'s3','a3')
        self.assertEqual(self.count('rejection'),0);self.assertEqual(self.count('reference'),1)

    def test_unassign_retains_face_and_human_history(self):
        self.human();s=session(self.ws,'unassign');decide(self.ws,s,[decision(s['source']['candidates'][0],'UNASSIGN')],'s2','a2')
        self.assertEqual(self.count('face'),1);self.assertEqual(self.count('event'),2)
        self.assertEqual(self.ws.inspect()['faces'][0]['state'],'UNASSIGNED')

    def test_invalid_cross_session_face_and_duplicate(self):
        s=self.seed();d=decision(s['source']['candidates'][0],'OUTSIDER')
        self.error('INVALID_DECISION',save_decisions,self.ws,s['session_id'],0,[dict(d,face_id='unknown')],'s')
        self.error('INVALID_DECISION',save_decisions,self.ws,s['session_id'],0,[d,d],'s')

    def test_wrong_expected_state_and_missing_person(self):
        s=self.seed();d=decision(s['source']['candidates'][0],'OUTSIDER')
        self.error('STALE_SOURCE',save_decisions,self.ws,s['session_id'],0,[dict(d,expected_state={'state':'IGNORED','person_id':None})],'s')
        self.error('INVALID_DECISION',save_decisions,self.ws,s['session_id'],0,[decision(s['source']['candidates'][0],'EXISTING_PERSON',person_id='unknown')],'s')

    def test_immutable_audit_rows(self):
        self.human()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ws.write() as c:c.execute("UPDATE event SET payload='{}'")


    def test_apply_process_death_rolls_back_and_retries(self):
        s=self.seed();save_decisions(self.ws,s['session_id'],0,[decision(s['source']['candidates'][0],'NEW_PERSON',display_name='Anonymous A',person_key='a',enroll_reference=True)],'s')
        b=self.ws.basis()
        code="from person_engine.public_v0 import Workspace,apply\nimport os\nw=Workspace("+repr(str(self.ws.home))+")\ndef fail(point,c):\n if point=='after_human_event': os._exit(73)\napply(w,"+repr(s['session_id'])+",1,'crash',fault=fail)"
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,73,result.stderr)
        reopened=Workspace(self.ws.home)
        self.assertEqual(reopened.basis(),b);self.assertEqual(self.count('event'),0)
        apply(reopened,s['session_id'],1,'crash');self.assertEqual(self.count('person'),1)

    def test_kernel_writer_excludes_concurrent_mutation(self):
        from person_engine.writer_lock import WriterLock
        with WriterLock(self.ws.home,'synthetic contention'):
            self.error('WRITER_BUSY',register,self.ws,request(self.ws,self.image,'a','r'),self.image)
        self.assertEqual(self.count('asset'),0)

    def test_correct_assignment_to_another_person(self):
        add(self.ws,self.image,'a',SyntheticProvider(((1,0,0),(0,1,0))))
        s=session(self.ws)
        r=decide(self.ws,s,[decision(f,'NEW_PERSON',display_name='Anonymous '+str(i),person_key='p'+str(i),enroll_reference=True) for i,f in enumerate(s['source']['candidates'])])
        s2=session(self.ws,'correction');f=s2['source']['candidates'][0]
        target=next(p for p in r['new_persons'].values() if p!=f['person_id'])
        decide(self.ws,s2,[decision(f,'CORRECTION',to={'state':'ASSIGNED','person_id':target})],'s2','a2')
        self.assertEqual(next(x for x in self.ws.inspect()['faces'] if x['face_id']==f['face_id'])['person_id'],target)
        with self.ws.read() as c:self.assertIsNone(c.execute('SELECT 1 FROM reference WHERE face_id=?',(f['face_id'],)).fetchone())

    def test_reject_does_not_clear_ignored_state(self):
        pid=self.human();s=session(self.ws,'ignore');decide(self.ws,s,[decision(s['source']['candidates'][0],'IGNORE_FACE')],'s2','a2')
        s2=session(self.ws,'reject');decide(self.ws,s2,[decision(s2['source']['candidates'][0],'REJECT',person_id=pid)],'s3','a3')
        self.assertEqual(self.ws.inspect()['faces'][0]['state'],'IGNORED')


    def test_person_crud_ids_and_replay(self):
        b=self.ws.basis();r=person_command(self.ws,'CREATE','create',b,display_name='Anonymous A')
        self.assertEqual(r,person_command(self.ws,'CREATE','create',b,display_name='Anonymous A'))
        pid=r['person_id'];person_command(self.ws,'RENAME','rename',self.ws.basis(),person_id=pid,display_name='Anonymous B')
        self.assertEqual(self.ws.inspect()['persons'][0]['person_id'],pid)
        person_command(self.ws,'RETIRE','retire',self.ws.basis(),person_id=pid)
        self.assertEqual(self.ws.inspect()['persons'][0]['status'],'retired')

    def test_person_crud_stale_and_duplicate_name(self):
        b=self.ws.basis();person_command(self.ws,'CREATE','create',b,display_name='Anonymous A')
        self.error('STALE_SOURCE',person_command,self.ws,'CREATE','stale',b,display_name='Anonymous B')
        self.error('CONFLICT',person_command,self.ws,'CREATE','duplicate',self.ws.basis(),display_name='Anonymous A')

    def test_person_retirement_rejects_active_assignment(self):
        pid=self.human()
        self.error('CONFLICT',person_command,self.ws,'RETIRE','retire',self.ws.basis(),person_id=pid)

    def test_renaming_person_invalidates_old_review(self):
        pid=self.human();s=session(self.ws,'pending');save_decisions(self.ws,s['session_id'],0,[decision(s['source']['candidates'][0],'UNASSIGN')],'s2')
        person_command(self.ws,'RENAME','rename',self.ws.basis(),person_id=pid,display_name='Anonymous B')
        self.error('STALE_SOURCE',apply,self.ws,s['session_id'],1,'a2')


class SnapshotTests(V0Case):
    def test_required_fields_and_path_free_json(self):
        self.human();r=publish_snapshot(self.ws,'p',self.ws.basis());doc=read_snapshot(self.ws,r['snapshot_id'])
        self.assertTrue({'workspace_id','snapshot_id','snapshot_version','schema_version','producer_version','persons','faces','assignments','ignored_state','provenance','source_digest','generated_at','compatibility'}<=set(doc))
        self.assertNotIn(str(self.root),canonical(doc));self.assertNotIn('/Users/',canonical(doc))

    def test_deterministic_replay_and_content_digest(self):
        self.seed();b=self.ws.basis();r=publish_snapshot(self.ws,'p',b)
        self.assertEqual(r,publish_snapshot(self.ws,'p',b));doc=read_snapshot(self.ws,r['snapshot_id'])
        self.assertEqual(r['content_digest'],digest(doc))
        self.assertEqual(r['payload_sha256'],hashlib.sha256((canonical(doc)+'\n').encode()).hexdigest())

    def test_versioned_immutable_publication(self):
        self.human();a=publish_snapshot(self.ws,'p',self.ws.basis());before=read_snapshot(self.ws,a['snapshot_id'])
        s=session(self.ws,'ignore');decide(self.ws,s,[decision(s['source']['candidates'][0],'IGNORE_FACE')],'s2','a2')
        b=publish_snapshot(self.ws,'p2',self.ws.basis())
        self.assertEqual(b['snapshot_version'],a['snapshot_version']+1)
        self.assertEqual(before,read_snapshot(self.ws,a['snapshot_id']))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ws.write() as c:c.execute('DELETE FROM snapshot')

    def test_stale_source_and_publish_key_conflict(self):
        s=self.seed();old=self.ws.basis();publish_snapshot(self.ws,'p',old)
        decide(self.ws,s,[decision(s['source']['candidates'][0],'OUTSIDER')])
        self.error('STALE_SOURCE',publish_snapshot,self.ws,'new',old)
        self.error('CONFLICT',publish_snapshot,self.ws,'p',self.ws.basis())
        self.assertEqual(publish_snapshot(self.ws,'p',old)['snapshot_version'],1)

    def test_publish_failure_no_partial_snapshot_or_receipt(self):
        self.seed();b=self.ws.basis()
        for point in ['after_snapshot_build','before_snapshot_commit']:
            with self.assertRaises(RuntimeError):publish_snapshot(self.ws,'p',b,fault=self.fail(point))
            self.assertEqual(self.count('snapshot'),0)
        self.assertEqual(publish_snapshot(self.ws,'p',b)['snapshot_version'],1)

    def test_snapshot_rechecks_source_before_commit(self):
        b=self.ws.basis()
        def mutation(point,c):
            if point=='after_snapshot_build':bump(c)
        self.error('STALE_SOURCE',publish_snapshot,self.ws,'p',b,fault=mutation)
        self.assertEqual(self.ws.basis(),b);self.assertEqual(self.count('snapshot'),0)

    def test_atomic_export_failure_retry_readback(self):
        self.human();r=publish_snapshot(self.ws,'p',self.ws.basis())
        with self.assertRaises(RuntimeError):export_snapshot(self.ws,r['snapshot_id'],fault=self.fail('before_output_commit'))
        self.assertEqual(list((self.ws.home/'exports').iterdir()),[])
        x=export_snapshot(self.ws,r['snapshot_id']);self.assertEqual(x,export_snapshot(self.ws,r['snapshot_id']))
        raw=(self.ws.home/x['relative_path']).read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),r['payload_sha256'])
        self.assertEqual(json.loads(raw),read_snapshot(self.ws,r['snapshot_id']))

    def test_export_does_not_overwrite_conflicting_bytes(self):
        r=publish_snapshot(self.ws,'p',self.ws.basis());x=export_snapshot(self.ws,r['snapshot_id'])
        target=self.ws.home/x['relative_path'];target.write_bytes(b'conflict')
        self.error('CONFLICT',export_snapshot,self.ws,r['snapshot_id']);self.assertEqual(target.read_bytes(),b'conflict')

    def test_export_only_published_id(self):
        self.error('NOT_FOUND',export_snapshot,self.ws,'unpublished')
        self.assertEqual(list((self.ws.home/'exports').iterdir()),[])

    def test_cross_workspace_snapshot_not_found(self):
        r=publish_snapshot(self.ws,'p',self.ws.basis());other=initialize(self.root/'other')
        self.error('NOT_FOUND',read_snapshot,other,r['snapshot_id'])


    def test_snapshot_process_death_before_commit(self):
        b=self.ws.basis()
        code="from person_engine.public_v0 import Workspace,publish_snapshot\nimport os\nw=Workspace("+repr(str(self.ws.home))+")\ndef fail(point,c):\n if point=='before_snapshot_commit': os._exit(74)\npublish_snapshot(w,'crash',w.basis(),fault=fail)"
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,74,result.stderr)
        reopened=Workspace(self.ws.home);self.assertEqual(self.count('snapshot'),0)
        self.assertEqual(publish_snapshot(reopened,'crash',b)['snapshot_version'],1)


class E2ETests(unittest.TestCase):
    def test_public_v0_synthetic_e2e(self):
        with tempfile.TemporaryDirectory(prefix='public_v0_e2e_') as td:
            self.assertEqual(end_to_end(td)['status'],'PASS')


if __name__=='__main__':unittest.main(verbosity=2)
