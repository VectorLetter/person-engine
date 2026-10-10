"""Verify an explicitly supplied external model hash. Never download or copy."""
import argparse,hashlib,json
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--path',required=True,type=Path)
    p.add_argument('--sha256',required=True)
    a=p.parse_args()
    if len(a.sha256)!=64 or any(c not in '0123456789abcdef' for c in a.sha256):
        p.error('Expected a lowercase SHA-256 digest from verified provenance')
    if a.path.is_symlink() or not a.path.is_file():
        print(json.dumps({'status':'MISSING_OR_UNSAFE','redistribution_verified':False}));return 1
    with a.path.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
    ok=actual==a.sha256
    print(json.dumps({'status':'MATCH' if ok else 'MISMATCH','sha256':actual,
                      'download_enabled':False,'redistribution_verified':False}))
    return int(not ok)
if __name__=='__main__':raise SystemExit(main())
