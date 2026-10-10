"""Step 3 bounded directory discovery and disposable-only admission."""
import hashlib,json,os,stat,tempfile
from pathlib import Path
from .core_io import FoundationError,digest,canonical,atomic_json,file_sha
from .registry_core import base_of,classify_variant,STILL_EXT_PRIORITY
VERSION='directory-ingest/1'
IMAGES={'.jpg','.jpeg','.png','.heic','.heif'}
SIDECARS={'.aae','.mov','.mp4'}
MAX_ITEMS=4096

def require(ok,code,message):
    if not ok:raise FoundationError(code,message)

def disposable(workspace,source=None):
    ws=Path(workspace).resolve();roots=(Path('/tmp').resolve(),Path(tempfile.gettempdir()).resolve())
    require(any(ws.is_relative_to(r) for r in roots),'DISPOSABLE_ONLY','Step 3 only permits marked disposable workspaces')
    marker=ws/'DISPOSABLE_INGEST.json';require(marker.is_file(),'DISPOSABLE_ONLY','Missing disposable marker')
    m=json.loads(marker.read_text());require(m.get('workspace')==str(ws),'DISPOSABLE_ONLY','Workspace marker mismatch')
    if source is not None:
        source=Path(source).resolve();require(any(source.is_relative_to(Path(r).resolve()) for r in m['fixture_roots']),'FIXTURE_ONLY','Source is not an authorized fixture root')
    return ws

def write_target(path,workspace):
    p=Path(path);require(not p.is_symlink() and p.resolve().is_relative_to(Path(workspace).resolve()),'DISPOSABLE_ONLY','Target escapes workspace')
    if p.is_file():require(p.stat().st_nlink==1,'DISPOSABLE_ONLY','Hard-linked target refused')
    return p

def signature(path):
    s=Path(path).stat();return {'device':s.st_dev,'inode':s.st_ino,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'ctime_ns':s.st_ctime_ns,'birthtime_ns':int(s.st_birthtime*1_000_000_000) if hasattr(s,'st_birthtime') else None}

def stable_hash(path,fault=None):
    require(stat.S_ISREG(Path(path).lstat().st_mode),'UNSUPPORTED','Not a regular source file')
    before=signature(path);h=hashlib.sha256()
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
    with os.fdopen(os.open(path,flags),'rb') as f:
        require(stat.S_ISREG(os.fstat(f.fileno()).st_mode),'UNSUPPORTED','Not a regular file')
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
            if fault:fault('hash_chunk',path)
    require(signature(path)==before,'SOURCE_CHANGED_DURING_INGEST',str(path))
    # A second streamed pass detects bytes changed with forged/restored mtime.
    require(file_sha(path)==h.hexdigest() and signature(path)==before,'SOURCE_CHANGED_DURING_INGEST',str(path))
    return h.hexdigest(),before

def verify_source(item):
    sha,s=stable_hash(item['normalized_path'])
    require(sha==item['content_sha256'] and s==item['stat_identity'],'SOURCE_CHANGED_DURING_INGEST',item['normalized_path'])

def discover(root,fault=None):
    root=Path(root).resolve();require(root.is_dir(),'INVALID_PATH','Ingest requires an existing directory')
    items=[]
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            p=Path(directory)/name;rel=str(p.relative_to(root))
            require(len(items)<MAX_ITEMS,'SCOPE_LIMIT','Directory exceeds fixed 4096-item limit; split the batch')
            ext=p.suffix.lower();group=str(Path(rel).parent/base_of(p.stem));status='DISCOVERED'
            row={'normalized_path':str(p),'relative_path':rel,'file_type':ext,'size':None,'source_group':group,'asset_intent':digest([str(root),group]),'content_intent':None,'content_sha256':None,'stat_identity':None,'discovery_status':status}
            try:
                require(not p.is_symlink(),'UNSUPPORTED','Symlinks are not source items')
                sha,s=stable_hash(p,fault);row.update(content_sha256=sha,content_intent=digest(['content',sha]),stat_identity=s,size=s['size'])
                if ext not in IMAGES|SIDECARS:row['discovery_status']='UNSUPPORTED'
            except (OSError,FoundationError) as e:row.update(discovery_status=getattr(e,'code','FAILED'),error=str(e))
            items.append(row)
    return sorted(items,key=lambda x:x['relative_path'])

def groups(items):
    grouped={}
    for row in items:grouped.setdefault(row['source_group'],[]).append(row)
    result=[]
    for name,rows in sorted(grouped.items()):
        stills=[r for r in rows if r['file_type'] in IMAGES]
        for r in rows:r['variant']=classify_variant(str(Path(r['relative_path']).with_suffix('.heic')) if r['file_type']=='.heif' else r['relative_path'],bool(stills))
        originals=[r for r in stills if r['variant']=='original']
        candidates=originals or stills
        candidates.sort(key=lambda r:(STILL_EXT_PRIORITY.index(r['file_type'][1:]) if r['file_type'][1:] in STILL_EXT_PRIORITY else 99,r['relative_path']))
        result.append({'group':name,'item_id':digest(name),'resources':rows,'canonical':candidates[0]['relative_path'] if candidates else None})
    return result
