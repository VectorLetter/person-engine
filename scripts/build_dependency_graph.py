"""Build runtime closure solely from this repository and declared dependencies."""
import argparse,ast,hashlib,importlib.util,json,sys,tomllib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def build():
    config=tomllib.loads((ROOT/'pyproject.toml').read_text())
    declared={x.split('==')[0] for x in config['project']['dependencies']}
    distribution=config['tool']['public-v0']['import-distributions']
    modules={}
    for p in (ROOT/'src/person_engine').rglob('*.py'):
        name='.'.join(p.relative_to(ROOT/'src').with_suffix('').parts)
        modules[name.removesuffix('.__init__')]=p
    entries=['person_engine','person_engine.__main__','person_engine.public_v0','person_engine.public_v0.__main__','person_engine.api','person_engine.api.__main__','person_engine.container_runtime','person_engine.import_tool']
    pending=entries[:];seen=set();edges={};external=set();errors=[];dynamic=[]
    while pending:
        name=pending.pop()
        if name in seen:continue
        seen.add(name)
        if name not in modules:errors.append({'module':name,'reason':'local module absent'});continue
        path=modules[name];package=name if path.name=='__init__.py' else name.rpartition('.')[0]
        imports=set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Import):imports.update(a.name for a in node.names)
            elif isinstance(node,ast.ImportFrom):
                base=importlib.util.resolve_name('.'*node.level+(node.module or ''),package) if node.level else node.module or ''
                imports.add(base);imports.update(base+'.'+a.name for a in node.names if base+'.'+a.name in modules)
            elif isinstance(node,ast.Call) and ast.unparse(node.func) in ('eval','exec','__import__','importlib.import_module','runpy.run_path','runpy.run_module'):
                dynamic.append({'path':str(path.relative_to(ROOT)),'line':node.lineno})
        local=sorted(x for x in imports if x=='person_engine' or x.startswith('person_engine.'))
        edges[name]=local;pending.extend(local)
        for x in imports-set(local):
            top=x.split('.')[0];external.add(top)
            if top not in sys.stdlib_module_names and distribution.get(top) not in declared:
                errors.append({'module':top,'reason':'undeclared external dependency'})
        parts=name.split('.');pending.extend('.'.join(parts[:i]) for i in range(1,len(parts)))
    paths=sorted(str(modules[m].relative_to(ROOT)) for m in seen if m in modules)
    return {'schema':'public-v0-runtime-closure/1','status':'PASS' if not errors and not dynamic else 'BLOCKED',
            'entry_points':entries,'runtime_modules':sorted(seen),'source_edges':edges,'external_top_level':sorted(external),
            'import_distributions':distribution,'source_digests':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
            'violations':errors,'dynamic_code_calls':dynamic,
            'resources':['fresh generated SQLite schema/workspace','user-selected media and external model interface','explicit config','macOS sips and best-effort process metadata'],
            'limitations':['Static closure is supplemented by runtime allowlist and external-access denial tests.','No model quality or redistribution approval is implied.']}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);a=p.parse_args();g=build()
    a.output.write_text(json.dumps(g,indent=2)+'\n');print(g['status'],len(g['runtime_modules']),'runtime modules')
    raise SystemExit(g['status']!='PASS')
