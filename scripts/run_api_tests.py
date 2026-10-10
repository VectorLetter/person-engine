"""Guarded ASGI tests and optional real loopback HTTP proof; anonymous data only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def live_e2e():
    import socket
    import threading
    import time
    import httpx
    import uvicorn
    from person_engine.api import create_app
    from public_v0_fixture import SyntheticProvider
    from api_fixture import end_to_end
    with tempfile.TemporaryDirectory(prefix='anonymous_api_live_') as td:
        root = Path(td).resolve() / 'root'
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(root, provider=SyntheticProvider()),
                                             host='127.0.0.1', log_config=None, access_log=False,
                                             proxy_headers=False, log_level='critical'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started, 'Loopback service did not start'
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=20) as client:
                assert client.get('/health').json() == {'status': 'ok'}
                result = end_to_end(client, root)
                result['transport'] = 'real loopback TCP HTTP'
                return result
        finally:
            server.should_exit = True
            thread.join(10)
            sock.close()
            assert not thread.is_alive(), 'Local server did not stop'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--live', action='store_true', help='Also bind one ephemeral loopback port for HTTP E2E')
    args = parser.parse_args()
    from build_dependency_graph import build
    graph = build()
    assert graph['status'] == 'PASS', graph['violations']
    code = '''import importlib.abc,sys,unittest,json
allowed=set(ALLOWED)
class PublicOnly(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if (fullname=='person_engine' or fullname.startswith('person_engine.')) and fullname not in allowed:
   raise ImportError('PUBLIC_IMPORT_DENIED')
sys.meta_path.insert(0,PublicOnly())
class Recorded(unittest.TextTestResult):
 def __init__(self,*a,**kw):super().__init__(*a,**kw);self.names=[]
 def startTest(self,test):self.names.append(test.id());super().startTest(test)
r=unittest.TextTestRunner(verbosity=2,resultclass=Recorded).run(unittest.defaultTestLoader.loadTestsFromNames(['test_api', 'test_synthetic_acceptance']))
print(json.dumps({'tests_run':r.testsRun,'test_ids':r.names,'failures':len(r.failures),'errors':len(r.errors),'skipped':len(r.skipped)}))
sys.exit(not r.wasSuccessful())
'''.replace('ALLOWED', repr(graph['runtime_modules']))
    with tempfile.TemporaryDirectory(prefix='anonymous_api_guard_') as td:
        td = str(Path(td).resolve())
        env = {k: v for k, v in os.environ.items() if not k.startswith(('PE_', 'PERSON_ENGINE_', 'PYTHON', 'VIRTUAL_ENV'))}
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                   PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ('scripts/test_guard', 'scripts')),
                   PE_RELEASE_GUARD='1', PE_RELEASE_REPO=str(ROOT), PE_RELEASE_TEMP=td,
                   TMPDIR=td, TMP=td, TEMP=td,
                   PATH=str(Path(sys.executable).parent) + ':/usr/bin:/bin:/usr/sbin:/sbin')
        run = subprocess.run([sys.executable, '-B', '-c', code], cwd=td, env=env, capture_output=True, text=True)
    report = {'name': 'LOCAL_API_REGRESSION', 'status': 'PASS' if run.returncode == 0 else 'FAIL',
              'returncode': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr,
              'guard': 'Original release file/database/network audit guard plus public import closure',
              'runtime_source_digests': graph['source_digests']}
    if run.returncode == 0:
        report['summary'] = json.loads(run.stdout.strip().splitlines()[-1])
    if args.live and run.returncode == 0:
        try:
            report['PUBLIC_V0_API_SYNTHETIC_E2E'] = live_e2e()
        except Exception:
            report['status'] = 'FAIL'
            report['PUBLIC_V0_API_SYNTHETIC_E2E'] = {'status': 'FAIL', 'message': 'Loopback proof failed'}
            import traceback
            traceback.print_exc()
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(report['status'], report.get('summary', {}))
    if run.returncode:
        print(run.stderr[-16000:])
    return report['status'] != 'PASS'


if __name__ == '__main__':
    raise SystemExit(main())
