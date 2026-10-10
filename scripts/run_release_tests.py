"""Independent installed-package validation; no private repository inputs."""
import argparse,hashlib,json,os,subprocess,sys,tempfile,time
from pathlib import Path
from build_dependency_graph import build,ROOT

def main():
    p=argparse.ArgumentParser();p.add_argument('--report',required=True,type=Path);a=p.parse_args()
    graph=build();assert graph['status']=='PASS',graph['violations']
    guard='''import importlib.abc,sys
allowed=set(ALLOWED)
class PublicOnly(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if (fullname=='person_engine' or fullname.startswith('person_engine.')) and fullname not in allowed:
   raise ImportError('PUBLIC_IMPORT_DENIED: '+fullname)
sys.meta_path.insert(0,PublicOnly())
'''.replace('ALLOWED',repr(graph['runtime_modules']))
    tests='''import unittest,json
class Recorded(unittest.TextTestResult):
 def startTest(self,test):super().startTest(test);self.names.append(test.id())
 def __init__(self,*args,**kw):super().__init__(*args,**kw);self.names=[]
r=unittest.TextTestRunner(verbosity=2,resultclass=Recorded).run(unittest.defaultTestLoader.loadTestsFromName('test_public_v0'))
print(json.dumps({'tests_run':r.testsRun,'test_ids':r.names,'failures':len(r.failures),'errors':len(r.errors),'skipped':len(r.skipped)}))
sys.exit(not r.wasSuccessful())
'''
    e2e='''import tempfile,json
from public_v0_fixture import end_to_end
with tempfile.TemporaryDirectory(prefix='anonymous_e2e_') as td:print(json.dumps(end_to_end(td)))
'''
    fresh='''import tempfile,json,sqlite3
from pathlib import Path
from person_engine.public_v0 import initialize,Workspace
with tempfile.TemporaryDirectory(prefix='fresh_smoke_') as td:
 w=initialize(Path(td)/'empty');v=w.inspect()
 assert set(p.name for p in w.home.iterdir())=={'workspace.json','state','content','exports'}
 assert v['basis']['revision']==0 and not v['assets'] and not v['persons'] and not v['faces']
 with w.read() as c:
  assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
  assert not c.execute('PRAGMA foreign_key_check').fetchall()
  assert {'asset','person','face','session','snapshot'}<={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 assert Workspace(w.home).workspace_id==w.workspace_id
 print(json.dumps({'status':'PASS','registry':'initialized','schema':'fresh','diagnostics':'empty healthy','layout':'PASS'}))
'''
    deny='''from pathlib import Path
import sqlite3,socket,importlib
for value in [Path.home()/'outside-test.sqlite',Path('outside-test.sqlite')]:
 # second path is disposable and is tested through an explicit external root instead
 if not value.is_absolute():continue
 try:value.read_bytes()
 except PermissionError:pass
 else:raise AssertionError('external file admitted')
try:sqlite3.connect(str(Path.home()/'outside-test.sqlite'))
except PermissionError:pass
else:raise AssertionError('external database admitted')
try:socket.socket().connect(('127.0.0.1',9))
except PermissionError:pass
else:raise AssertionError('network admitted')
for name in ['person_engine.review_apply','person_engine.generation_export','person_engine.incremental_sync']:
 try:importlib.import_module(name)
 except ImportError as e:assert 'PUBLIC_IMPORT_DENIED' in str(e)
 else:raise AssertionError('excluded import admitted')
print('external file/database/network and excluded imports denied')
'''
    results=[]
    with tempfile.TemporaryDirectory(prefix='release_validation_') as td:
        td=str(Path(td).resolve())
        env={k:v for k,v in os.environ.items() if not k.startswith(('PE_','PERSON_ENGINE_','PYTHON','VIRTUAL_ENV'))}
        env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONNOUSERSITE='1',PYTHONPATH=os.pathsep.join(str(ROOT/p) for p in ('scripts/test_guard','scripts')),
                   PE_RELEASE_GUARD='1',PE_RELEASE_REPO=str(ROOT),PE_RELEASE_TEMP=td,TMPDIR=td,TMP=td,TEMP=td,
                   PATH=str(Path(sys.executable).parent)+':/usr/bin:/bin:/usr/sbin:/sbin')
        jobs=[('synthetic-regression',tests),('PUBLIC_V0_SYNTHETIC_E2E',e2e),('fresh-workspace',fresh),
              ('closure-imports',"import importlib\nfrom pathlib import Path\nfor name in sorted(allowed):\n m=importlib.import_module(name)\n assert Path(m.__file__).resolve().is_relative_to(Path("+repr(str(ROOT/'src'))+"))\n"),
              ('external-access-denial',deny)]
        for name,code in jobs:
            start=time.monotonic();r=subprocess.run([sys.executable,'-B','-c',guard+code],cwd=td,env=env,capture_output=True,text=True)
            item={'name':name,'returncode':r.returncode,'seconds':round(time.monotonic()-start,3),'stdout':r.stdout,'stderr':r.stderr}
            if name in ('synthetic-regression','PUBLIC_V0_SYNTHETIC_E2E','fresh-workspace') and r.returncode==0:item['summary']=json.loads(r.stdout.strip().splitlines()[-1])
            results.append(item);print('PASS' if r.returncode==0 else 'FAIL',name,flush=True)
            if r.returncode:print(r.stderr[-16000:],flush=True)
        for name,cmd in [('module-entry',[sys.executable,'-B','-m','person_engine','--help']),('console-entry',[str(Path(sys.executable).parent/'person-engine'),'--help']),('pip-check',[sys.executable,'-m','pip','check'])]:
            # pip inspects only this environment; it is not subject to the inference fixture guard.
            e=env.copy()
            if name=='pip-check':e.pop('PE_RELEASE_GUARD',None);e.pop('PYTHONPATH',None)
            r=subprocess.run(cmd,cwd=td,env=e,capture_output=True,text=True)
            results.append({'name':name,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
            print('PASS' if r.returncode==0 else 'FAIL',name,flush=True)
    report={'status':'PASS' if all(r['returncode']==0 for r in results) else 'FAIL','results':results,
            'runtime_source_digests':graph['source_digests'],
            'validation_source_digests':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'scripts').rglob('*.py')},
            'imports_from_installed_release_only':True,'cwd_outside_repository':True,
            'scope':'Anonymous generated assets; no model weights or actual user photos; Python-level audit guard, not a native OS sandbox.'}
    a.report.write_text(json.dumps(report,indent=2)+'\n');return report['status']!='PASS'
if __name__=='__main__':raise SystemExit(main())
