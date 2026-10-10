"""Per-content versioned inference, fixed limits, resumable row checkpoints."""
import json,os,subprocess,sys
from pathlib import Path
import cv2,numpy as np
from .core_io import atomic_json,file_sha,digest,canonical,fsync_dir
from .ingest_support import require,verify_source
from .detection_identity import _face_id
from .scan import sniff_magic
from .image_decode import decode_jpeg_png
LIMITS={'max_source_bytes':64*1024*1024,'max_pixels':24_000_000,'max_faces':256,'embedding_block':1}
CONFIG={'confidence':0.6,'nms':0.3,'top_k':5000,'adapter':'incremental-preprocess/1','limits':LIMITS}

class Models:
    def __init__(self,models):
        self.models=Path(models);self.versions={'detector_sha256':file_sha(self.models/'face_detection_yunet_2023mar.onnx'),'embedding_sha256':file_sha(self.models/'glintr100.onnx'),'config':CONFIG}
        self.detector=None;self.embedder=None;self.detect_calls=0;self.embed_calls=0
    def detect(self,img):
        if self.detector is None:
            require(file_sha(self.models/'face_detection_yunet_2023mar.onnx')==self.versions['detector_sha256'],'MODEL_CHANGED','Detector bytes changed after version freeze')
            from .detect import YuNetDetector
            self.detector=YuNetDetector(str(self.models/'face_detection_yunet_2023mar.onnx'),conf_threshold=CONFIG['confidence'])
        self.detect_calls+=1;return self.detector.detect(img)
    def embed(self,img,row):
        if self.embedder is None:
            require(file_sha(self.models/'glintr100.onnx')==self.versions['embedding_sha256'],'MODEL_CHANGED','Embedding model bytes changed after version freeze')
            from .embed import AuraFaceEmbedder
            self.embedder=AuraFaceEmbedder(self.models/'glintr100.onnx')
        self.embed_calls+=1;crop=self.embedder.align_crop(img,row);return self.embedder.embed(crop),crop

def decode(path,temporary):
    """Magic-first JPEG/PNG on all platforms; native HEIC is isolated."""
    path=Path(path)
    require(path.stat().st_size<=LIMITS['max_source_bytes'],'CONTENT_LIMIT','Image file exceeds fixed size cap')
    kind=sniff_magic(path).format
    if kind in ('jpeg','png'):
        return decode_jpeg_png(path,max_bytes=LIMITS['max_source_bytes'],max_pixels=LIMITS['max_pixels'])
    require(kind=='heic','UNSUPPORTED_MEDIA','Only JPEG/PNG or native HEIC decode is supported')
    require(sys.platform=='darwin','HEIC_PROVIDER_REQUIRED','HEIC requires a native provider')
    return _decode_native_heic(path,temporary)


def _decode_native_heic(path,temporary):
    # Only this non-Public-V0 branch uses macOS sips, including dimension preflight.
    info=subprocess.run(['sips','-g','pixelWidth','-g','pixelHeight',str(path)],capture_output=True,text=True,timeout=30)
    try:
        wh={x.split(':')[0].strip():int(x.split(':')[1]) for x in info.stdout.splitlines() if 'pixelWidth:' in x or 'pixelHeight:' in x}
        require(0<wh['pixelWidth']*wh['pixelHeight']<=LIMITS['max_pixels'],'CONTENT_LIMIT','Image exceeds fixed pixel cap')
    except (ValueError,KeyError):raise ValueError('Invalid HEIC image header') from None
    src=Path(temporary)/'decoded.jpg'
    r=subprocess.run(['sips','-s','format','jpeg',str(path),'--out',str(src)],capture_output=True,text=True,timeout=60)
    require(r.returncode==0,'DECODE_FAILED','Native HEIC conversion failed')
    img=cv2.imread(str(src),cv2.IMREAD_COLOR)
    require(img is not None,'DECODE_FAILED','Native HEIC decode failed')
    require(img.shape[0]*img.shape[1]<=LIMITS['max_pixels'],'CONTENT_LIMIT','Decoded pixel cap')
    return img

def validate(directory,sha,versions):
    directory=Path(directory);seal=json.loads((directory/'COMMITTED.json').read_text())
    require(seal['content_sha256']==sha and seal['versions']==versions,'CACHE_VERSION','Cache binding differs')
    for rel,h in seal['files'].items():
        f=(directory/rel).resolve();require(f.is_relative_to(directory.resolve()) and file_sha(f)==h,'CACHE_DIGEST','Cache artifact digest mismatch')
    d=json.loads((directory/'detect.json').read_text());faces=d['faces'];require(d['content_sha256']==sha and len(faces)<=LIMITS['max_faces'],'CACHE_MAPPING','Detection binding')
    with np.load(directory/'embeddings.npz',allow_pickle=False) as data:a=data['auraface']
    require(a.shape==(len(faces),512) and np.isfinite(a).all() and (len(a)==0 or (np.linalg.norm(a,axis=1)>0).all()),'CACHE_EMBEDDING','Invalid embedding rows')
    detver='yunet:'+digest([versions['detector_sha256'],versions['config']])
    require([f['face_id'] for f in faces]==seal['face_ids'] and all(f['face_idx']==i and f['face_id']==_face_id(sha,detver,i) for i,f in enumerate(faces)),'CACHE_MAPPING','Exact row/face mapping differs')
    return {'faces':faces,'cache_dir':str(directory),'cache_digest':file_sha(directory/'COMMITTED.json'),'compute_version':digest(versions),'content_sha256':sha,'artifacts':[{'path':str(directory/rel),'sha256':h} for rel,h in seal['files'].items()]+[{'path':str(directory/'COMMITTED.json'),'sha256':file_sha(directory/'COMMITTED.json')}]}

def preprocess(item,root,models,fault=None):
    verify_source(item);sha=item['content_sha256'];version=digest(models.versions);base=Path(root)/'ingest_content'/sha;dest=base/version;temp=base/(version+'.partial')
    require(dest.resolve().is_relative_to(Path(root).resolve()) and temp.resolve().is_relative_to(Path(root).resolve()),'DISPOSABLE_ONLY','Cache target escapes workspace')
    if dest.exists():return {**validate(dest,sha,models.versions),'cache_hit':True}
    temp.mkdir(parents=True,exist_ok=True);checkpoint=temp/'checkpoint.json';state=json.loads(checkpoint.read_text()) if checkpoint.exists() else {'content_sha256':sha,'versions':models.versions,'rows':{},'detection_sha256':None}
    require(state['content_sha256']==sha and state['versions']==models.versions,'CACHE_VERSION','Checkpoint version conflict')
    img=decode(item['normalized_path'],temp)
    if state['detection_sha256']:
        require(file_sha(temp/'detection.json')==state['detection_sha256'],'CACHE_DIGEST','Detection checkpoint changed');rows=json.loads((temp/'detection.json').read_text())
    else:
        rows=[list(map(float,r)) for r in models.detect(img)];require(len(rows)<=LIMITS['max_faces'],'CONTENT_LIMIT','Too many faces; no partial publish')
        require(all(len(r)==15 and np.isfinite(r).all() for r in rows),'DETECTION_INVALID','Invalid detection rows')
        atomic_json(temp/'detection.json',rows);state['detection_sha256']=file_sha(temp/'detection.json');atomic_json(checkpoint,state)
    if fault:fault('detection_checkpoint',None)
    faces=[];embs=[];detver='yunet:'+digest([models.versions['detector_sha256'],models.versions['config']]);(temp/'crops').mkdir(exist_ok=True)
    for i,row in enumerate(rows):
        fid=_face_id(sha,detver,i);embpath=temp/f'row_{i}.npy';cropfile=temp/'crops'/f'{fid}.jpg'
        if str(i) in state['rows']:
            saved=state['rows'][str(i)];require(file_sha(embpath)==saved['embedding'] and file_sha(cropfile)==saved['crop'],'CACHE_DIGEST','Embedding checkpoint changed');v=np.load(embpath,allow_pickle=False)
        else:
            v,crop=models.embed(img,np.asarray(row,dtype=np.float32));v=np.asarray(v,dtype=np.float32);require(v.shape==(512,) and np.isfinite(v).all() and np.linalg.norm(v)>0,'CACHE_EMBEDDING','Invalid embedding')
            np.save(embpath,v);require(cv2.imwrite(str(cropfile),crop),'CACHE_WRITE','Crop write failed');state['rows'][str(i)]={'embedding':file_sha(embpath),'crop':file_sha(cropfile)};atomic_json(checkpoint,state)
        embs.append(v);faces.append({'face_id':fid,'face_idx':i,'bbox':row[:4],'det_score':row[14],'face_w':row[2],'crop':str(dest/'crops'/f'{fid}.jpg')})
        if fault:fault('embedding_checkpoint',i)
    verify_source(item)
    atomic_json(temp/'detect.json',{'content_sha256':sha,'faces':faces});np.savez(temp/'embeddings.npz',auraface=np.stack(embs) if embs else np.zeros((0,512),dtype=np.float32))
    files={str(f.relative_to(temp)):file_sha(f) for f in temp.rglob('*') if f.is_file() and f.name!='COMMITTED.json'}
    atomic_json(temp/'COMMITTED.json',{'content_sha256':sha,'versions':models.versions,'files':files,'face_ids':[f['face_id'] for f in faces]})
    validate(temp,sha,models.versions);fsync_dir(temp)
    if fault:fault('cache_before_rename',None)
    os.rename(temp,dest);fsync_dir(base)
    if fault:fault('cache_after_rename',None)
    return {**validate(dest,sha,models.versions),'cache_hit':False}
