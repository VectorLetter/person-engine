"""Disposable Docker acceptance orchestrator; independent client uses HTTP + import CLI.

Run with a separate client environment containing httpx and jsonschema, but no
person-engine installation. No formal media, models or databases are inputs.
Evidence is local-only and must stay outside the build context.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]


def fingerprint(root):
    result = {}
    for file in sorted(root.rglob('*')):
        if file.is_file():
            info=file.stat()
            result[str(file.relative_to(root))]={'size':info.st_size,'mtime_ns':info.st_mtime_ns,
                'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--client-python',required=True)
    p.add_argument('--docker',default='docker')
    p.add_argument('--image',default='person-engine:local')
    p.add_argument('--skip-build',action='store_true',help='Reuse local image, still verify installed source bytes')
    a=p.parse_args()
    out=a.output.resolve()
    if out.is_relative_to(REPO) or (out.exists() and any(out.iterdir())):
        p.error('Use a new empty evidence directory outside the repository')
    out.mkdir(parents=True,exist_ok=True)
    for folder in ('workspace','photos','models','client','contract'):(out/folder).mkdir()
    client=out/'client'; project='pe-accept-'+out.name.lower().replace('_','-')[-30:]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    env={**os.environ,'PERSON_ENGINE_WORKSPACE_PATH':str(out/'workspace'),
         'PERSON_ENGINE_PHOTO_PATH':str(out/'photos'),'PERSON_ENGINE_MODEL_PATH':str(out/'models'),
         'PERSON_ENGINE_UID':str(os.getuid()),'PERSON_ENGINE_GID':str(os.getgid()),
         'PERSON_ENGINE_PORT':str(port),'PERSON_ENGINE_PROVIDER':'real','PERSON_ENGINE_IMAGE':a.image}
    if os.getuid()==0:
        p.error('Run acceptance as a non-root local operator')
    compose=[a.docker,'compose','--project-directory',str(REPO),'--env-file',os.devnull,
             '-f',str(REPO/'docker-compose.yml'),'-p',project]
    events=[];checks=[]
    def check(name,ok,detail=None):
        checks.append({'name':name,'pass':bool(ok),'detail':detail})
        (out/'runtime-checks.json').write_text(json.dumps(checks,indent=2))
        if not ok:raise AssertionError(name)
    def run(command,*,label=None,expected=0):
        started=datetime.now(timezone.utc).isoformat()
        result=subprocess.run(command,env=env,capture_output=True,text=True)
        events.append({'command':command,'started_at':started,'completed_at':datetime.now(timezone.utc).isoformat(),'returncode':result.returncode})
        (out/'executions.json').write_text(json.dumps(events,indent=2))
        if label:(out/(label+'.log')).write_text(result.stdout+result.stderr)
        if result.returncode!=expected:raise RuntimeError(f'Command failed ({result.returncode}): {label or command[0]}\n'+result.stderr[-3000:])
        return result.stdout
    def execute(code):
        return run([*compose,'exec','-T','person-engine','python','-c',code])
    def get(path):
        with urllib.request.urlopen(f'http://127.0.0.1:{port}'+path,timeout=3) as response:
            return json.load(response)
    def wait():
        for _ in range(120):
            try:
                if get('/health')=={'status':'ok'}:return
            except OSError:pass
            time.sleep(.5)
        raise RuntimeError('Container did not become healthy')
    def post(path,body):
        request=urllib.request.Request(f'http://127.0.0.1:{port}'+path,
            data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)['data']
    def consumer(phase):
        # Client has no package import or filesystem access to the service workspace.
        command=[a.client_python,'-I',str(REPO/'scripts/docker_http_consumer.py'),phase,
            '--base-url',f'http://127.0.0.1:{port}','--output',str(client),
            '--contract',str(out/'contract/openapi.json'),'--fixtures',str(out/'photos'),
            '--import-command',str(out/'import-command.json'),'--forbid-workspace',str(out/'workspace')]
        run(command,label='consumer-'+phase)
        results=json.loads((client/'evidence'/f'{phase}-checks.json').read_text())
        check('independent consumer '+phase,all(x['pass'] for x in results),{'checks':len(results)})
    completed=False
    try:
        config=json.loads(run([*compose,'config','--format','json'],label='compose-config'))
        service=config['services']['person-engine']
        mounts={m['target']:m for m in service['volumes']}
        check('compose config valid',len(config['services'])==1)
        check('host exposure localhost only',all(x['host_ip']=='127.0.0.1' for x in service['ports']))
        check('compose mounts',mounts['/media/photos']['read_only'] and mounts['/models']['read_only'] and not mounts['/data/person-engine'].get('read_only',False))
        if not a.skip_build:run([*compose,'build'],label='build')
        image=json.loads(run([a.docker,'image','inspect',a.image],label='image-inspect'))[0]
        check('image linux arm64',image['Os']=='linux' and image['Architecture']=='arm64',image['Id'])
        check('image nonroot default',image['Config']['User']=='10001:10001')
        fixture_code="""import cv2,numpy as np,base64,json
out={}
for value in (32,64,160,192,224):
 for suffix in (('.jpg','.png') if value==32 else ('.png',)):
  ok,data=cv2.imencode(suffix,np.full((96,96,3),value,np.uint8));assert ok
  out[str(value)+suffix]=base64.b64encode(data).decode()
print(json.dumps(out))"""
        fixtures=json.loads(run([a.docker,'run','--rm','--network','none','--entrypoint','python',a.image,'-c',fixture_code]))
        for name,data in fixtures.items():(out/'photos'/name).write_bytes(base64.b64decode(data))
        before=fingerprint(out/'photos'); (out/'source-before.json').write_text(json.dumps(before,indent=2))
        for name in ('openapi.json','public-v0-api-contract.md','public-v0-error-codes.md','public-v0-limitations.md'):
            source=REPO/'docs'/name
            shutil.copyfile(source,out/'contract'/name)
        run([*compose,'up','-d','--no-build'],label='real-up');wait()
        check('default health',get('/health')=={'status':'ok'})
        w=post('/v1/workspaces',{'workspace_key':'default-no-fallback'})
        diagnostic=get('/v1/workspaces/'+w['workspace_id']+'/diagnostics')['data']
        check('default no synthetic fallback',diagnostic['model_availability']=='MODEL_UNAVAILABLE' and diagnostic['calibration_status']=='PUBLIC_CALIBRATION_GAP' and 'provider=synthetic_acceptance' not in diagnostic['warnings'],diagnostic)
        run([*compose,'down'],label='real-down')
        env['PERSON_ENGINE_PROVIDER']='synthetic_acceptance'
        run([*compose,'up','-d','--no-build'],label='synthetic-up');wait()
        identifier=run([*compose,'ps','-q','person-engine']).strip()
        inspect=json.loads(run([a.docker,'inspect',identifier],label='container-inspect'))[0]
        check('actual port localhost only',inspect['HostConfig']['PortBindings']['8000/tcp']==[{'HostIp':'127.0.0.1','HostPort':str(port)}])
        actual_mounts={m['Destination']:m['RW'] for m in inspect['Mounts']}
        check('actual volume permissions',actual_mounts=={'/data/person-engine':True,'/media/photos':False,'/models':False})
        check('container binds all interfaces',execute("from pathlib import Path;print(any(line.split()[1]=='00000000:1F40' and line.split()[3]=='0A' for line in Path('/proc/net/tcp').read_text().splitlines()[1:]))").strip()=='True')
        check('runtime nonroot',int(execute('import os;print(os.getuid())'))!=0)
        code="""from pathlib import Path
import errno,json,os
result={}
for root in ('/media/photos','/models'):
 try:
  with (Path(root)/'must-not-create').open('xb') as f:f.write(b'forbidden')
 except OSError as error:result[root]={'errno':error.errno,'read_only':error.errno==errno.EROFS}
 else:raise AssertionError('mount accepted a write')
p=Path('/data/person-engine/runtime/write-proof');p.write_bytes(b'allowed');assert p.read_bytes()==b'allowed';p.unlink()
print(json.dumps(result))"""
        permission=json.loads(execute(code));(out/'mount-write-attempts.json').write_text(json.dumps(permission,indent=2))
        check('source mount write rejected',permission['/media/photos']['read_only'])
        check('model mount write rejected',permission['/models']['read_only'])
        check('workspace writable',True)
        # Compare installed modules, not a host source mount.
        expected={str(p.relative_to(REPO/'src/person_engine')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (REPO/'src/person_engine').rglob('*.py')}
        actual=json.loads(execute("import person_engine,hashlib,json;from pathlib import Path;r=Path(person_engine.__file__).parent;print(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in r.rglob('*.py')}))"))
        check('installed package matches current source',actual==expected)
        import_command=[*compose,'exec','-T','person-engine','python','-m','person_engine.import_tool',
                        '--root','/data/person-engine/service','--source-root','/media/photos']
        (out/'import-command.json').write_text(json.dumps(import_command))
        # Real CLI idempotency/collision/escape proofs against anonymous inputs.
        w=post('/v1/workspaces',{'workspace_key':'import-boundaries'})
        command=[*import_command,'--source-directory',w['source_directory']]
        first=json.loads(run([*command,'--source','32.jpg','--destination','one.jpg']))
        retry=json.loads(run([*command,'--source','32.jpg','--destination','one.jpg']))
        conflict=json.loads(run([*command,'--source','64.png','--destination','one.jpg'],expected=3))
        check('import JPEG copy',first['status']=='COPIED')
        check('import retry same receipt',retry=={**first,'status':'REUSED'})
        check('import conflict',conflict['status']=='CONFLICT')
        failure=json.loads(run([*command,'--source','32.jpg','--destination','../escape.jpg'],expected=2))
        check('import traversal blocked',failure['error']['code']=='UNSAFE_PATH')
        consumer('initial')
        records=json.loads((client/'evidence/initial-http.json').read_text())
        successful={(r['method'],r['template']) for r in records if r['status']<400}
        check('20 endpoints successful',len(successful)==20,sorted(successful))
        check('JPEG and PNG ingested',any(r['name']=='register-A' and r['request']['body']['media_type']=='image/jpeg' for r in records) and any(r['name']=='register-B' and r['request']['body']['media_type']=='image/png' for r in records))
        check('live frozen API equal',get('/openapi.json')==json.loads((out/'contract/openapi.json').read_text()))
        run([*compose,'logs','--no-color'],label='service-before-recreate')
        run([*compose,'down'],label='synthetic-remove')
        run([*compose,'up','-d','--no-build'],label='synthetic-recreate');wait()
        new_identifier=run([*compose,'ps','-q','person-engine']).strip()
        check('container actually recreated',new_identifier!=identifier,{'before':identifier,'after':new_identifier})
        consumer('restart')
        after=fingerprint(out/'photos');(out/'source-after.json').write_text(json.dumps(after,indent=2))
        check('source file set and bytes and metadata unchanged',before==after)
        check('models stayed empty',list((out/'models').iterdir())==[])
        # Wait for Docker's actual liveness check (not model readiness).
        for _ in range(60):
            health=json.loads(run([a.docker,'inspect',new_identifier]))[0]['State']['Health']['Status']
            if health=='healthy':break
            time.sleep(.5)
        check('Docker healthcheck healthy',health=='healthy')
        completed=True
    finally:
        run([*compose,'logs','--no-color'],label='service-final')
        run([*compose,'down'],label='cleanup')
        (out/'summary.json').write_text(json.dumps({'status':'PASS' if completed and checks and all(x['pass'] for x in checks) else 'FAIL','checks':checks},indent=2))
    print('Docker synthetic acceptance PASS',len(checks),'checks; evidence:',out)


if __name__=='__main__':
    main()
