"""Inspect every saved final-image layer; do not rely on a source-tree scan alone.

Operator deny terms (JSON strings) are optional local input and are reported only
as digests. This is a bounded pattern/artifact audit, not universal secret detection.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ARTIFACTS={'.jpg','.jpeg','.png','.heic','.heif','.gif','.webp','.mp4','.db','.sqlite','.sqlite3',
           '.onnx','.npz','.npy','.pt','.pth','.safetensors','.pkl'}
# .pth also names Python import-configuration text, handled separately below.
SECRET=re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_-]{20,}')
PERSONAL=re.compile(rb'(?:/Users/|/home/)[A-Za-z0-9_.-]+/')


def scan(archive,deny):
    findings=[];layers=[];counts={'files':0,'bytes':0};upstream_paths=[]
    with tarfile.open(archive,'r:*') as image:
        manifest=json.load(image.extractfile('manifest.json'))
        for entry in manifest:
            config=json.load(image.extractfile(entry['Config']))
            for field in ('config','history'):
                text=json.dumps(config.get(field,{})).encode()
                for term in deny:
                    if term.encode().lower() in text.lower():findings.append({'layer':'config','kind':'private_context','digest':hashlib.sha256(term.encode()).hexdigest()})
            for layer in entry['Layers']:
                count=0
                with tarfile.open(fileobj=image.extractfile(layer),mode='r|*') as filesystem:
                    for member in filesystem:
                        name=member.name.lstrip('./'); path=Path(name)
                        if '.git' in path.parts or path.name in ('.env','id_rsa','id_ed25519'):
                            findings.append({'layer':layer,'path':name,'kind':'git_or_secret_file'})
                        if not member.isfile():continue
                        count+=1;counts['files']+=1;counts['bytes']+=member.size
                        file_findings_start=len(findings); upstream_start=len(upstream_paths)
                        digest=hashlib.sha256()
                        if path.suffix.lower() in ARTIFACTS and not (path.suffix=='.pth' and member.size<65536):
                            findings.append({'layer':layer,'path':name,'kind':'media_model_or_state_artifact'})
                        if path.name.endswith(('-wal','-shm','-journal')) or 'haarcascade' in path.name:
                            findings.append({'layer':layer,'path':name,'kind':'state_or_cascade'})
                        stream=filesystem.extractfile(member)
                        first=stream.read(65536)
                        if first.startswith((b'SQLite format 3',b'\x89PNG\r\n',b'\xff\xd8\xff',b'\x93NUMPY')):
                            findings.append({'layer':layer,'path':name,'kind':'data_magic'})
                        # Streaming all bytes also covers binary strings and boundary matches.
                        tail=b'';block=first;seen=set()
                        while block:
                            digest.update(block)
                            data=tail+block
                            for term in deny:
                                if term.encode().lower() in data.lower() and term not in seen:
                                    seen.add(term);findings.append({'layer':layer,'path':name,'kind':'private_context','digest':hashlib.sha256(term.encode()).hexdigest()})
                            if SECRET.search(data) and 'secret' not in seen:
                                seen.add('secret');findings.append({'layer':layer,'path':name,'kind':'secret_pattern'})
                            if PERSONAL.search(data) and 'personal' not in seen:
                                seen.add('personal')
                                # Installed upstream binaries/code sometimes retain their own build paths.
                                # Record them for review; first-party paths always fail.
                                record={'layer':layer,'path':name,'kind':'upstream_build_path_requires_review'}
                                if name.startswith('opt/person-engine/') or '/site-packages/person_engine/' in name:findings.append(record)
                                else:upstream_paths.append(record)
                            tail=data[-4096:];block=stream.read(1024*1024)
                        for item in findings[file_findings_start:]+upstream_paths[upstream_start:]:item['sha256']=digest.hexdigest()
                layers.append({'layer':layer,'files':count})
    return {'status':'PASS' if not findings and not upstream_paths else 'REVIEW_REQUIRED','layers':layers,'counts':counts,
            'findings':findings,'upstream_build_path_occurrences':upstream_paths,
            'coverage':['all saved final-image layers, including overwritten/deleted content','artifact filenames','media/database/array magic','model weights/cascades','Git/secret files','full-byte private-context and secret patterns','image config/history'],
            'limitations':'Upstream build-path occurrences need separate provenance review. No universal secret-detection claim; no inference-quality or licensing approval.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--docker',default='docker');p.add_argument('--denylist',type=Path)
    p.add_argument('--review',type=Path,help='Local explicit reviewed exceptions: exact path/kind/SHA-256/reason')
    a=p.parse_args()
    deny=json.loads(a.denylist.read_text()) if a.denylist else []
    with tempfile.TemporaryDirectory(prefix='image_layer_audit_') as temporary:
        archive=Path(temporary)/'image.tar'
        subprocess.run([a.docker,'image','save','--output',str(archive),a.image],check=True)
        report=scan(archive,deny)
    if a.review:
        accepted=json.loads(a.review.read_text())
        candidates=report['findings']+report['upstream_build_path_occurrences']
        reviewed=[];unresolved=[]
        for item in candidates:
            match=next((row for row in accepted if row.get('reason') and all(row.get(k)==item.get(k) for k in ('path','kind','sha256')) and item.get('sha256')),None)
            (reviewed if match else unresolved).append({**item,**({'reason':match['reason']} if match else {})})
        report.update(reviewed_findings=reviewed,unresolved_findings=unresolved,
                      status='PASS' if not unresolved else 'BLOCKED')
    report['image_id']=subprocess.check_output([a.docker,'image','inspect','--format','{{.Id}}',a.image],text=True).strip()
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(report['status'],report['counts'],'findings',len(report['findings']))
    raise SystemExit(report['status']!='PASS')


if __name__=='__main__':main()
