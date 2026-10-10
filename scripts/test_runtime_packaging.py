"""Anonymous import boundary and container configuration regression tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from person_engine import import_tool as tool
from person_engine import container_runtime as runtime
from test_image_decode import fixture_bytes

ROOT = Path(__file__).resolve().parents[1]


class Imports(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='anonymous_import_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base/'service'; self.source = self.base/'photos'
        self.imports = self.root/'slot_test/imports'
        self.imports.mkdir(parents=True); self.source.mkdir()
        (self.root/'api-root.json').write_text(json.dumps({'schema':'local-fastapi-root/v1'}))
        (self.imports.parent/'workspace.json').write_text(json.dumps({'contract':'person-engine-public-v0/1','workspace_id':'ws_test'}))
        f = fixture_bytes()
        for name in ('synthetic.jpg','synthetic.png','color.png'):
            (self.source/name).write_bytes(f[name])

    def copy(self, source='synthetic.png', destination=None, hint='slot_test/imports'):
        return tool.copy_import(self.root, hint, self.source, source, destination)

    def denied(self, **kw):
        with self.assertRaises((ValueError, OSError)):
            self.copy(**kw)

    def test_jpeg(self):
        receipt = self.copy('synthetic.jpg')
        self.assertEqual(receipt['status'], 'COPIED')
        self.assertEqual(receipt['media_type'], 'image/jpeg')
        self.assertEqual((self.imports/receipt['source_path']).read_bytes(), (self.source/'synthetic.jpg').read_bytes())

    def test_png(self):
        receipt = self.copy()
        self.assertEqual(receipt['status'], 'COPIED')
        self.assertEqual(receipt['media_type'], 'image/png')
        self.assertEqual(receipt['source_path'], receipt['content_sha256']+'.png')

    def test_retry(self):
        first=self.copy(); p=self.imports/first['source_path']; before=p.stat()
        second=self.copy()
        self.assertEqual(second, {**first,'status':'REUSED'})
        self.assertEqual(p.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(p.stat().st_ino, before.st_ino)

    def test_same_named_content(self):
        self.copy(destination='chosen.png')
        self.assertEqual(self.copy(destination='chosen.png')['status'], 'REUSED')

    def test_collision(self):
        first=self.copy(destination='chosen.png'); before=(self.imports/'chosen.png').read_bytes()
        receipt=self.copy('color.png', 'chosen.png')
        self.assertEqual(receipt['status'], 'CONFLICT')
        self.assertEqual((self.imports/'chosen.png').read_bytes(), before)
        self.assertEqual(hashlib.sha256(before).hexdigest(), first['content_sha256'])

    def test_larger_existing_destination_conflicts(self):
        target=self.imports/'large.png'
        with target.open('wb') as stream:stream.truncate(tool.MAX_BYTES+1)
        before=target.stat()
        self.assertEqual(self.copy(destination='large.png')['status'],'CONFLICT')
        self.assertEqual(target.stat().st_mtime_ns,before.st_mtime_ns)
        self.assertEqual(target.stat().st_size,before.st_size)

    def test_invalid_workspace_marker(self):
        (self.imports.parent/'workspace.json').write_text('[]')
        with self.assertRaisesRegex(tool.ImportFailure,'INVALID_WORKSPACE'):
            self.copy()

    def test_traversal(self):
        for value in ('../escape.png','/absolute.png','a/../b.png','a//b.png','./a.png','a\\b.png'):
            with self.subTest(value=value):
                self.denied(source=value);self.denied(destination=value)
        self.denied(hint='../imports'); self.denied(hint='slot_test/state')

    def test_symlink_source(self):
        (self.source/'link.png').symlink_to(self.source/'synthetic.png')
        self.denied(source='link.png')
        (self.source/'linked').symlink_to(self.source, target_is_directory=True)
        self.denied(source='linked/synthetic.png')

    def test_symlink_destination(self):
        (self.imports/'link').symlink_to(self.source, target_is_directory=True)
        self.denied(destination='link/untouched.png')
        (self.imports/'file.png').symlink_to(self.source/'synthetic.png')
        self.denied(destination='file.png')
        self.assertFalse((self.source/'untouched.png').exists())

    def test_hardlink_destination(self):
        os.link(self.source/'synthetic.png', self.imports/'hard.png')
        self.denied(destination='hard.png')

    def test_source_missing(self):
        self.denied(source='absent.png')
        self.assertFalse(list(self.imports.iterdir()))

    def test_destination_boundary(self):
        receipt=self.copy(destination='nested/a.png')
        self.assertTrue((self.imports/receipt['source_path']).resolve().is_relative_to(self.imports))
        self.denied(hint='absent/imports')
        (self.root/'api-root.json').write_text('{}')
        self.denied()

    def test_source_unchanged(self):
        def capture():
            return {p.name:(p.stat().st_size,p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.source.iterdir()}
        before=capture();self.copy();self.copy('synthetic.jpg');self.copy()
        self.assertEqual(capture(), before)

    def test_receipt_no_absolute_path(self):
        receipt=json.dumps(self.copy())
        self.assertNotIn(str(self.base),receipt)
        self.assertEqual(set(json.loads(receipt)), {'source_reference','source_path','content_sha256','bytes','media_type','status'})

    def test_magic_routing(self):
        (self.source/'misnamed.heic').write_bytes((self.source/'synthetic.jpg').read_bytes())
        self.assertEqual(self.copy('misnamed.heic')['media_type'],'image/jpeg')
        (self.source/'actual.heic').write_bytes(b'\0\0\0\x18ftypheic'+b'\0'*16)
        with self.assertRaisesRegex(tool.ImportFailure,'UNSUPPORTED_MEDIA'):
            self.copy('actual.heic')

    def test_size_limit(self):
        with patch.object(tool,'MAX_BYTES',1):
            # read_file's explicit limit also protects marker reads; test direct bound.
            with tool.ExitStack() as stack:
                fd=tool.absolute_directory(stack,self.source)
                with self.assertRaisesRegex(tool.ImportFailure,'CONTENT_LIMIT'):
                    tool.read_file(fd,'synthetic.png',1)

    def test_failure_cleanup(self):
        with patch.object(tool.os,'link',side_effect=OSError('test fault')):
            self.denied()
        self.assertEqual(list(self.imports.iterdir()),[])


class Entrypoint(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='anonymous_runtime_');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        for name in ('photos','models'):(self.base/name).mkdir()
        self.env={'PERSON_ENGINE_HOME':str(self.base/'home'),'PERSON_ENGINE_PHOTO_ROOT':str(self.base/'photos'),'PERSON_ENGINE_MODEL_ROOT':str(self.base/'models')}
        p=patch.dict(os.environ);p.start();self.addCleanup(p.stop)

    def test_default_no_fallback(self):
        config=runtime.configure(self.env)
        self.assertIsNone(config['provider']);self.assertEqual(config['host'],'0.0.0.0')
        self.assertEqual(config['root'],self.base/'home/service')
        self.assertEqual(config['port'],8000)

    def test_explicit_synthetic(self):
        config=runtime.configure({**self.env,'PERSON_ENGINE_PROVIDER':'synthetic_acceptance'})
        self.assertIsInstance(config['provider'],runtime.SyntheticAcceptanceProvider)

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):runtime.configure({**self.env,'PERSON_ENGINE_PROVIDER':'auto'})

    def test_invalid_model_config_no_fallback(self):
        (self.base/'models/provider.json').write_text('{}')
        with self.assertRaises(KeyError):runtime.configure(self.env)

    def test_model_path_escape(self):
        (self.base/'models/provider.json').write_text(json.dumps({'detector_path':'../outside','embedding_path':'embed'}))
        with self.assertRaises(ValueError):runtime.configure(self.env)

    def test_runtime_writes_inside_home(self):
        runtime.configure(self.env)
        self.assertEqual({p.name for p in (self.base/'home').iterdir()},{'runtime'})
        self.assertTrue(Path(os.environ['TMPDIR']).is_relative_to(self.base/'home'))

    def test_native_binding_unchanged(self):
        from person_engine.api import __main__ as native
        with patch('sys.argv',['person-engine-api']),patch.object(native.uvicorn,'run') as run,patch.object(native,'create_app'):
            native.main()
        self.assertEqual(run.call_args.kwargs['host'],'127.0.0.1')

    def test_docker_static(self):
        docker=(ROOT/'Dockerfile').read_text(); compose=(ROOT/'docker-compose.yml').read_text()
        self.assertNotIn('--platform=linux/arm64',docker)
        self.assertIn('USER 10001:10001',docker)
        self.assertIn('127.0.0.1:${PERSON_ENGINE_PORT:-8000}:8000',compose)
        self.assertIn('/media/photos:ro',compose);self.assertIn('/models:ro',compose)
        self.assertIn('read_only: true',compose)
        self.assertTrue((ROOT/'.dockerignore').read_text().splitlines()[1]=='**')
        self.assertEqual(len([x for x in (ROOT/'.env.example').read_text().splitlines() if x]),5)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    suite=unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    report={'status':'PASS' if result.wasSuccessful() else 'FAIL','tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}
    a.report.write_text(json.dumps(report,indent=2)+'\n')
    raise SystemExit(not result.wasSuccessful())
