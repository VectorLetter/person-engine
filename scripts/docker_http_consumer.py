"""Independent HTTP consumer. Only four frozen public documents and HTTP are inputs."""
import argparse
import hashlib
import importlib.metadata
import json
import re
import struct
import sys
import subprocess
import time
import zlib
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator, FormatChecker

parser = argparse.ArgumentParser(description='Independent Docker HTTP consumer; no service imports or DB reads')
parser.add_argument('phase', choices=['initial', 'restart'])
parser.add_argument('--base-url', required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--contract', type=Path, required=True)
parser.add_argument('--fixtures', type=Path, required=True)
parser.add_argument('--import-command', type=Path, required=True, help='JSON argv prefix for explicit import CLI')
parser.add_argument('--forbid-workspace', type=Path, required=True)
options = parser.parse_args()
ROOT = options.output.resolve()
(ROOT/'evidence').mkdir(parents=True, exist_ok=True)
SPEC = json.loads(options.contract.read_text())
IMPORT_COMMAND = json.loads(options.import_command.read_text())
GUARD_EVENTS = []


def guard(event, args):
    if event == 'import' and str(args[0]).split('.')[0] == 'person_engine':
        GUARD_EVENTS.append(event)
        raise RuntimeError('Forbidden service import')
    if event == 'sqlite3.connect':
        GUARD_EVENTS.append(event)
        raise RuntimeError('Forbidden database access')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = str(args[0])
        mode = str(args[1])
        if str(options.forbid_workspace.resolve()) in path or path.endswith(('.sqlite', '.sqlite3', '.db')):
            GUARD_EVENTS.append({'event': event, 'path': path})
            raise RuntimeError('Forbidden repository/state/database access')


sys.addaudithook(guard)


class Consumer:
    def __init__(self, phase):
        self.phase = phase
        self.client = httpx.Client(base_url=options.base_url, trust_env=False, timeout=20)
        self.records = []
        self.checks = []

    def check(self, name, ok, detail=None):
        self.checks.append({'name': name, 'pass': bool(ok), 'detail': detail})

    def call(self, name, method, template, *, ids=None, body=None, key=None,
             status=200, code=None, query=None, extra_headers=None):
        path = template.format(**(ids or {}))
        rid = f'{self.phase}-{len(self.records)+1:03d}'
        headers = {'X-Request-ID': rid}
        if key is not None:
            headers['Idempotency-Key'] = key
        headers.update(extra_headers or {})
        r = self.client.request(method, path, json=body, params=query, headers=headers)
        payload = r.json()
        rec = {'name': name, 'method': method, 'template': template, 'path': path,
               'request': {'headers': headers, 'body': body, 'query': query},
               'status': r.status_code, 'headers': dict(r.headers), 'response': payload,
               'raw_sha256': hashlib.sha256(r.content).hexdigest()}
        self.records.append(rec)
        expected = [status] if isinstance(status, int) else status
        self.check(name + ':status', r.status_code in expected, {'expected': expected, 'actual': r.status_code})
        self.check(name + ':request_id_header', r.headers.get('X-Request-ID') == rid)
        if isinstance(payload, dict) and 'request_id' in payload:
            self.check(name + ':request_id_body', payload['request_id'] == rid)
        if code:
            self.check(name + ':error_code', payload.get('code') == code, payload.get('code'))
        self.check(name + ':no_response_path_or_traceback', not re.search(
            r'Traceback|/Users/|/private/|/var/folders/|registry\.sqlite|site-packages|File "', r.text))
        if r.status_code >= 400:
            self.check(name + ':safe_error_shape', set(payload) == {'code','message','request_id','details'}
                       and payload['details'] is None)
        operation = SPEC['paths'].get(template, {}).get(method.lower(), {})
        response_schema = operation.get('responses', {}).get(str(r.status_code), {}).get(
            'content', {}).get('application/json', {}).get('schema')
        if response_schema:
            schema = {**response_schema, 'components': SPEC['components']}
            errors = [e.message for e in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload)]
            self.check(name + ':openapi_schema', not errors, errors)
        else:
            self.check(name + ':documented_status', False, r.status_code)
        print(name, r.status_code, payload.get('code') or payload.get('data', {}).get('state', 'OK'), flush=True)
        (ROOT/'evidence'/f'{self.phase}-http.json').write_text(json.dumps(self.records, indent=2))
        return payload.get('data', payload), r

    def get(self, name, suffix='', **kw):
        return self.call(name, 'GET', '/v1/workspaces/{workspace_id}'+suffix, ids=self.ids, **kw)[0]

    def post(self, name, suffix, body, key, **kw):
        return self.call(name, 'POST', '/v1/workspaces/{workspace_id}'+suffix,
                         ids=self.ids, body=body, key=key, **kw)[0]

    def poll(self, name, job):
        states = [job['state']]
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            job = self.get(name, '/jobs/{job_id}')
            states.append(job['state'])
            if job['state'] in {'SUCCEEDED','FAILED','BLOCKED'}:
                break
            time.sleep(.05)
        self.check(name+':terminal', job['state'] in {'SUCCEEDED','FAILED','BLOCKED'}, states)
        self.check(name+':terminal_timestamp', job.get('finished_at') is not None)
        self.check(name+':stage_not_fake_percent', job['progress'] in {'complete','stopped'})
        self.check(name+':calibration_warning', 'PUBLIC_CALIBRATION_GAP' in job['diagnostics'])
        if job.get('error'):
            self.check(name+':safe_job_error', job['error'].get('details') is None and bool(job['error'].get('request_id')))
        return job

    def save(self):
        installed = {d.metadata['Name']:d.version for d in importlib.metadata.distributions()}
        self.check('no_service_package_installed', not any('person-engine' in n for n in installed))
        self.check('no_service_module_loaded', not any(n.startswith('person_engine') for n in sys.modules))
        self.check('no_guard_violations', not GUARD_EVENTS, GUARD_EVENTS)
        (ROOT/'evidence'/f'{self.phase}-checks.json').write_text(json.dumps(self.checks, indent=2))
        (ROOT/'evidence'/'consumer-dependencies.json').write_text(json.dumps(installed, indent=2))
        print('CHECKS', len(self.checks), 'FAILED', [x for x in self.checks if not x['pass']], flush=True)


def fixture_png(value):
    def chunk(kind, body):
        return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body)&0xffffffff)
    pixels=(b'\x00'+bytes([value,value,value])*96)*96
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',96,96,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')


POLICY={'match_similarity':.8,'match_margin':.1,'cluster_similarity':.8}


def run(c, assets, key):
    body={'asset_ids':assets,'expected_basis':c.get(key+'_basis')['basis'],'policy':POLICY}
    job=c.post(key,'/ingest-runs',body,key,status=202)
    c.ids['job_id']=job['job_id']
    terminal=c.poll(key+'_poll',job)
    c.check(key+'_succeeded',terminal['state']=='SUCCEEDED',terminal)
    return terminal,body


def register(c,label,value,key):
    # A is JPEG; remaining fixtures are PNG. Both traverse the real decoder.
    suffix = '.jpg' if label == 'A' else '.png'
    filename = str(value) + suffix
    data = (options.fixtures/filename).read_bytes()
    imported = subprocess.run([*IMPORT_COMMAND, '--source-directory', c.source_directory,
                               '--source', filename, '--destination', label+suffix],
                              check=True, capture_output=True, text=True)
    receipt = json.loads(imported.stdout)
    c.check(key+':explicit_import', receipt['status'] in ('COPIED','REUSED') and
            receipt['content_sha256'] == hashlib.sha256(data).hexdigest() and receipt['bytes'] == len(data))
    body={'source_namespace':'synthetic-acceptance','source_ref':label,'source_path':receipt['source_path'],
          'content_sha256':receipt['content_sha256'],'bytes':receipt['bytes'],'media_type':receipt['media_type'],
          'expected_revision':c.get(key+'_basis')['basis']['revision']}
    asset=c.post(key,'/assets',body,key,status=201)
    return asset,body


def projections(c,prefix):
    return {kind:c.get(prefix+'_'+kind,'/'+kind)['items'] for kind in ['people','faces','assignments']}


def publish(c,key):
    body={'expected_basis':c.get(key+'_basis')['basis']}
    receipt=c.post(key,'/snapshots',body,key,status=201)
    c.ids['snapshot_id']=receipt['snapshot_id']
    doc=c.get(key+'_read','/snapshots/{snapshot_id}')
    exported,r=c.call(key+'_export','GET','/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/export',ids=c.ids)
    c.check(key+'_export_json_matches',doc==exported)
    c.check(key+'_export_hash',hashlib.sha256(r.content).hexdigest()==receipt['payload_sha256'] and r.content.endswith(b'\n'))
    c.check(key+'_source_basis',doc['source_digest']==body['expected_basis']['source_digest']==receipt['source_digest'] and doc['provenance']['basis']==body['expected_basis'])
    c.check(key+'_calibration_not_validated',doc['compatibility']['calibration_status']=='PUBLIC CALIBRATION GAP')
    (ROOT/'evidence'/(key+'-export.json')).write_bytes(r.content)
    return receipt,body,doc


def lifecycle(c,name,extras):
    w,_=c.call(name+'_create_workspace','POST','/v1/workspaces',body={'workspace_key':name},status=201)
    c.ids={'workspace_id':w['workspace_id']}
    relative=Path(w['source_directory']);assert not relative.is_absolute() and '..' not in relative.parts
    c.source_directory=w['source_directory']
    basis0=w['basis']
    c.check(name+'_workspace_chain',c.get(name+'_read_workspace')==w)
    empty=projections(c,name+'_empty');c.check(name+'_fresh_empty_roster',all(not v for v in empty.values()))
    assets={};requests={};registrations={}
    for label,value in [('A',32),('B',64),('C',160),('D',224),('E',192)]:
        a,b=register(c,label,value,'register-'+label);assets[label]=a['asset_id'];requests[label]=b;registrations[label]=a
    reverse={v:k for k,v in assets.items()}
    before=c.get(name+'_before_register_replay')['basis']
    again=c.post(name+'_registration_replay','/assets',requests['A'],'register-A')
    c.check(name+'_registration_same_receipt',again['receipt']==registrations['A']['receipt'] and again['replay'])
    c.check(name+'_registration_no_revision',c.get(name+'_after_register_replay')['basis']==before)
    job,runbody=run(c,list(assets.values()),'run-main')
    jobid=job['job_id']
    again=c.post(name+'_run_replay','/ingest-runs',runbody,'run-main')
    c.check(name+'_same_job',again==job)
    r=c.post(name+'_review_create','/reviews',{'run_id':job['result_refs']['run_id']},'review-main',status=201)
    c.ids['review_id']=r['session_id']
    c.check(name+'_review_get',c.get(name+'_review_read','/reviews/{review_id}')==r)
    candidates=c.get(name+'_candidates','/reviews/{review_id}/candidates')['items']
    by_label={reverse[x['asset_id']]:x for x in candidates}
    c.check(name+'_four_valid_candidates',set(by_label)=={'A','B','C','E'})
    c.check(name+'_no_machine_authority',all(x['state']=='UNKNOWN' and x['person_id'] is None and x['candidate_person_id'] is None and x['authority']=='NON_AUTHORITATIVE' and x['scores']==[] for x in candidates))
    groups=sorted(sorted(reverse[next(x['asset_id'] for x in candidates if x['face_id']==fid)] for fid in g['face_ids']) for g in r['source']['groups'])
    c.check(name+'_synthetic_groups',groups==[['A','B'],['C'],['E']],groups)
    pre=projections(c,name+'_inferred')
    c.check(name+'_inference_not_human_facts',not pre['people'] and not pre['assignments'] and len(pre['faces'])==4)
    c.check(name+'_D_no_faces',c.get(name+'_D_filter','/faces',query={'asset_id':assets['D']})['total']==0)
    repeated,_=run(c,[],'match-repeat')
    repeated_review=c.post(name+'_repeat_review','/reviews',{'run_id':repeated['result_refs']['run_id']},'repeat-review',status=201)
    c.check(name+'_stable_run_candidates_same_basis',repeated['result_refs']['run_id']==job['result_refs']['run_id'] and repeated_review['source']==r['source'])
    decisions=[]
    for label in ['A','B','C','E']:
        candidate=by_label[label]
        d={'face_id':candidate['face_id'],'action':'NEW_PERSON' if label in ('A','B') else 'OUTSIDER' if label=='C' else 'IGNORE_FACE',
           'expected_state':{'state':candidate['state'],'person_id':candidate['person_id']}}
        if label in ('A','B'):d.update(display_name='Synthetic A',person_key='synthetic-a',enroll_reference=True)
        decisions.append(d)
    decision_body={'revision':r['revision'],'decisions':decisions}
    saved=c.post(name+'_save_valid','/reviews/{review_id}/decisions',decision_body,'save-main')
    c.check(name+'_saved_revision',saved['session_id']==r['session_id'] and saved['revision']==r['revision']+1)
    dup=c.post(name+'_decision_replay','/reviews/{review_id}/decisions',decision_body,'save-main')
    c.check(name+'_decision_replay_same_receipt',dup==saved)
    c.check(name+'_decision_replay_no_revision',c.get(name+'_review_after_replay','/reviews/{review_id}')['revision']==saved['revision'])
    c.post(name+'_stale_decision','/reviews/{review_id}/decisions',decision_body,'stale-save',status=409,code='STALE_REVISION')
    c.post(name+'_invalid_decision','/reviews/{review_id}/decisions',{'revision':saved['revision'],'decisions':[]},'bad-save',status=422,code='INVALID_DECISION')
    c.check(name+'_save_no_human_facts',projections(c,name+'_saved')['people']==[])
    applybody={'revision':saved['revision']}
    applied=c.post(name+'_apply','/reviews/{review_id}/apply',applybody,'apply-main')
    c.check(name+'_apply_outcome',c.records[-1]['headers'].get('x-apply-outcome')=='SUCCESS')
    c.check(name+'_one_human_person',set(applied['new_persons'])=={'synthetic-a'} and len(set(applied['event_ids']))==len(applied['event_ids'])>0)
    pid=applied['new_persons']['synthetic-a']
    basis=c.get(name+'_applied_basis')['basis'];facts=projections(c,name+'_applied')
    c.check(name+'_person_read',facts['people']==[{'person_id':pid,'display_name':'Synthetic A','status':'active'}])
    c.ids['person_id']=pid
    c.check(name+'_person_detail',c.get(name+'_person_detail_read','/people/{person_id}')==facts['people'][0])
    expected={'A':'ASSIGNED','B':'ASSIGNED','C':'OUTSIDER','E':'IGNORED'}
    c.check(name+'_face_human_states',{reverse[f['asset_id']]:f['state'] for f in facts['faces']}==expected)
    c.check(name+'_assignment_facts',len(facts['assignments'])==2 and all(x['person_id']==pid and x['event_id'] in applied['event_ids'] for x in facts['assignments']))
    for key in ['apply-main','apply-new-key']:
        replay=c.post(name+'_'+key+'_replay','/reviews/{review_id}/apply',applybody,key)
        c.check(name+'_'+key+'_same_receipt',replay==applied and c.records[-1]['headers'].get('x-apply-outcome')=='IDEMPOTENT_REPLAY')
    c.check(name+'_apply_no_duplicate_mutation',c.get(name+'_after_apply_replays')['basis']==basis and projections(c,name+'_after_replays')==facts)
    receipt,pubbody,doc=publish(c,name+'-publication')
    c.check(name+'_snapshot_facts',doc['persons']==facts['people'] and doc['faces']==facts['faces'] and doc['assignments']==facts['assignments'])
    c.check(name+'_snapshot_version',receipt['snapshot_version']==1)
    c.check(name+'_ignored_state',sum(x['ignored'] for x in doc['ignored_state'])==1 and next(x for x in doc['ignored_state'] if x['ignored'])['face_id']==by_label['E']['face_id'])
    inf=doc['provenance']['inference']
    c.check(name+'_inference_provenance',len(inf)==5 and {reverse[i['asset_id']]:i['status'] for i in inf}=={k:'NO_FACES' if k=='D' else 'INGESTED' for k in assets} and all(i['manifest']['kind']=='synthetic-fixture' and i['manifest']['embedding_model']=='synthetic-acceptance-vectors-v1' and i['manifest']['calibration_status']=='SYNTHETIC_ONLY' for i in inf))
    c.check(name+'_human_event_provenance',len(doc['provenance']['human_events'])==len(applied['event_ids']))
    same=c.post(name+'_snapshot_replay','/snapshots',pubbody,name+'-publication',status=201)
    c.check(name+'_same_publication',same==receipt and c.get(name+'_snapshot_list','/snapshots')['total']==1)
    replaydoc,response=c.call(name+'_export_replay','GET','/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/export',ids=c.ids)
    c.check(name+'_export_same_bytes',hashlib.sha256(response.content).hexdigest()==receipt['payload_sha256'] and replaydoc==doc)
    d=c.get(name+'_diagnostics','/diagnostics')
    c.check(name+'_synthetic_markers',{'provider=synthetic_acceptance','mode=test/acceptance','SYNTHETIC_ONLY_NO_REAL_MODEL_VALIDATION','PUBLIC_CALIBRATION_GAP','REAL_MODEL_SMOKE_SKIPPED'}<=set(d['warnings']) and d['calibration_status']=='PUBLIC_CALIBRATION_GAP' and d['model_availability']=='CONFIGURED')
    normalized={'groups':groups,'candidates':{k:{field:v[field] for field in ['geometry','state','person_id','candidate_person_id','scores','comparison','authority','model_space','detection_id']} for k,v in by_label.items()},
                'persons':[(p['display_name'],p['status']) for p in doc['persons']],
                'faces':{reverse[f['asset_id']]:{'geometry':f['geometry'],'state':f['state'],'person':'Synthetic A' if f['person_id'] else None} for f in doc['faces']},
                'assignment_labels':sorted(reverse[next(f['asset_id'] for f in doc['faces'] if f['face_id']==a['face_id'])] for a in doc['assignments']),
                'inference':{reverse[x['asset_id']]:{'manifest':x['manifest'],'status':x['status'],'face_count':len(x['face_ids'])} for x in inf}}
    state={'ids':dict(c.ids),'old_job_id':jobid,'run_body':runbody,'review_id':r['session_id'],'saved':saved,'applied':applied,'apply_body':applybody,
           'pub_key':name+'-publication','pub_body':pubbody,'receipt':receipt,'snapshot':doc,'basis':basis,'normalized':normalized}
    if extras:
        c.call('missing_workspace','GET','/v1/workspaces/{workspace_id}',ids={'workspace_id':'missing-workspace'},status=404,code='WORKSPACE_NOT_FOUND')
        c.post('asset_binding_conflict','/assets',{**requests['B'],'source_ref':'A','expected_revision':basis['revision']},'asset-conflict',status=409,code='ASSET_CONFLICT')
        c.post('invalid_source_path','/assets',{**requests['A'],'source_path':'../outside.png'},'unsafe',status=422,code='UNSAFE_PATH')
        c.post('missing_source_path','/assets',{**requests['A'],'source_ref':'missing','source_path':'missing.png','expected_revision':basis['revision']},'missing-source',status=409,code='SOURCE_CHANGED')
        c.post('stale_snapshot','/snapshots',{'expected_basis':basis0},'stale-publish',status=409,code='STALE_REVISION')
        c.call('missing_snapshot','GET','/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}',ids={**c.ids,'snapshot_id':'missing-snapshot'},status=404,code='NOT_FOUND')
        # Public optional existing-person trial: new logical A source, same known anonymous pixels.
        a,_=register(c,'A2',32,'register-A2');second,_=run(c,[a['asset_id']],'run-existing')
        rev=c.post('review_existing','/reviews',{'run_id':second['result_refs']['run_id']},'review-existing',status=201)
        c.ids['review_id']=rev['session_id']
        candidate=next(x for x in c.get('existing_candidates','/reviews/{review_id}/candidates')['items'] if x['asset_id']==a['asset_id'])
        c.check('existing_person_machine_proposal',candidate['state']=='UNKNOWN' and candidate['candidate_person_id']==pid and candidate['comparison']=='SINGLE_REFERENCE_PERSON_NO_MARGIN_EVIDENCE')
        ds={'revision':rev['revision'],'decisions':[{'face_id':candidate['face_id'],'action':'EXISTING_PERSON','person_id':pid,'expected_state':{'state':candidate['state'],'person_id':candidate['person_id']}}]}
        saved2=c.post('save_existing','/reviews/{review_id}/decisions',ds,'save-existing')
        c.post('apply_existing','/reviews/{review_id}/apply',{'revision':saved2['revision']},'apply-existing')
        finalfacts=projections(c,'existing_applied')
        c.check('existing_person_explicit_assignment',len(finalfacts['people'])==1 and len(finalfacts['faces'])==5 and len(finalfacts['assignments'])==3)
        latest,_,latestdoc=publish(c,'existing-publication')
        c.check('snapshot_next_version',latest['snapshot_version']==2 and len(latestdoc['assignments'])==3)
        # Original applied revision remains replayable even after unrelated domain advancement.
        c.ids['review_id']=state['review_id'];c.ids['snapshot_id']=receipt['snapshot_id']
        old=c.post('old_apply_after_new_facts','/reviews/{review_id}/apply',applybody,'apply-main')
        c.check('old_apply_stays_noop',old==applied and c.records[-1]['headers'].get('x-apply-outcome')=='IDEMPOTENT_REPLAY')
        c.check('old_snapshot_immutable',c.get('old_snapshot_after_new_facts','/snapshots/{snapshot_id}')==doc)
        state.update(final_basis=c.get('final_basis_before_restart')['basis'],final_facts=finalfacts,final_publication_count=2)
    return state


def initial(c):
    c.call('health','GET','/health')
    first=lifecycle(c,'independent-one',True)
    second=lifecycle(c,'independent-two',False)
    c.check('two_fresh_workspaces_deterministic',first['normalized']==second['normalized'])
    (ROOT/'evidence/determinism.json').write_text(json.dumps({'equal':first['normalized']==second['normalized'],'first':first['normalized'],'second':second['normalized']},indent=2))
    (ROOT/'evidence/restart-input.json').write_text(json.dumps(first,indent=2))
    schema=c.client.get('/openapi.json');c.check('live_frozen_openapi_equal',schema.status_code==200 and schema.json()==SPEC)
    (ROOT/'evidence/live-openapi.json').write_text(json.dumps(schema.json(),indent=2))


def restart(c):
    state=json.loads((ROOT/'evidence/restart-input.json').read_text());c.ids=state['ids'];c.ids['job_id']=state['old_job_id']
    c.call('health_after_restart','GET','/health')
    c.get('old_job_lost','/jobs/{job_id}',status=404,code='JOB_NOT_FOUND')
    c.check('durable_basis',c.get('basis_after_restart')['basis']==state['final_basis'])
    c.check('durable_human_facts',projections(c,'after_restart')==state['final_facts'])
    # Exact old body may now be stale, but the old in-memory key must not resurrect its job.
    job=c.post('same_key_new_lifetime','/ingest-runs',state['run_body'],'run-main',status=202)
    c.check('new_job_identity',job['job_id']!=state['old_job_id'])
    c.get('old_job_still_lost','/jobs/{job_id}',status=404,code='JOB_NOT_FOUND')
    c.ids['job_id']=job['job_id'];terminal=c.poll('new_job_poll',job)
    c.check('stale_source_job_actionable',terminal['state']=='BLOCKED' and terminal['error']['code']=='STALE_REVISION')
    new,_=run(c,[],'deliberate-post-restart-match')
    c.check('synthetic_restart_still_usable',new['state']=='SUCCEEDED')
    c.ids['review_id']=state['review_id'];c.ids['snapshot_id']=state['receipt']['snapshot_id']
    applied=c.post('apply_replay_after_restart','/reviews/{review_id}/apply',state['apply_body'],'apply-main')
    c.check('durable_apply_receipt',applied==state['applied'] and c.records[-1]['headers'].get('x-apply-outcome')=='IDEMPOTENT_REPLAY')
    receipt=c.post('publish_replay_after_restart','/snapshots',state['pub_body'],state['pub_key'],status=201)
    c.check('durable_snapshot_receipt',receipt==state['receipt'])
    exported,response=c.call('export_after_restart','GET','/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/export',ids=c.ids)
    c.check('durable_export_bytes', exported==state['snapshot'] and hashlib.sha256(response.content).hexdigest()==state['receipt']['payload_sha256'])
    c.check('durable_snapshot_content',c.get('snapshot_after_restart','/snapshots/{snapshot_id}')==state['snapshot'])
    c.check('no_new_publication_after_restart',c.get('snapshot_count_after_restart','/snapshots')['total']==state['final_publication_count'])
    c.check('no_restart_replay_mutation',c.get('basis_after_restart_replays')['basis']==state['final_basis'] and projections(c,'after_restart_replays')==state['final_facts'])
    d=c.get('diagnostics_after_restart','/diagnostics');c.check('synthetic_markers_after_restart',{'provider=synthetic_acceptance','mode=test/acceptance','PUBLIC_CALIBRATION_GAP'}<=set(d['warnings']))


if __name__=='__main__':
    c=Consumer(options.phase)
    # Refuse to mutate an unrelated server.
    assert c.client.get('/openapi.json').json()==SPEC
    assert c.client.get('/health').json()=={'status':'ok'}
    try:
        (initial if options.phase=='initial' else restart)(c)
    finally:
        c.save()
    raise SystemExit(any(not x['pass'] for x in c.checks))
