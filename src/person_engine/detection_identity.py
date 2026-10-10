"""Stable detection identity from content, model contract and row index."""
import hashlib
def _face_id(content_sha: str, detector_version: str, face_idx: int) -> str:
    return 'K' + hashlib.sha256(f'{content_sha}|{detector_version}|{face_idx}'.encode()).hexdigest()[:12]
