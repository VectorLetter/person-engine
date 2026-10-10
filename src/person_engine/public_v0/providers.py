"""Explicit local model interface. No weights, downloads or calibrated defaults."""
from pathlib import Path
import math

from ..core_io import file_sha
from .store import require, exact_keys, token


def validate_manifest(m):
    exact_keys(m, ('kind', 'detector_sha256', 'embedding_sha256', 'embedding_model', 'embedding_dim',
                   'detector_config', 'preprocess_version', 'calibration_status'))
    require(m['kind'] in ('local-model', 'synthetic-fixture'), 'MODEL_CONTRACT', 'Unknown provider kind')
    for key in ('detector_sha256', 'embedding_sha256'):
        require(isinstance(m[key], str) and len(m[key]) == 64 and all(x in '0123456789abcdef' for x in m[key]),
                'MODEL_CONTRACT', 'Invalid model digest')
    token(m['embedding_model']); token(m['preprocess_version'])
    require(type(m['embedding_dim']) is int and 1 <= m['embedding_dim'] <= 4096, 'MODEL_CONTRACT', 'Invalid dimension')
    cfg = m['detector_config']
    exact_keys(cfg, ('confidence', 'nms', 'top_k'))
    require(all(type(cfg[k]) in (int, float) and math.isfinite(cfg[k]) and 0 <= cfg[k] <= 1
                for k in ('confidence', 'nms')) and type(cfg['top_k']) is int and 1 <= cfg['top_k'] <= 10000,
            'CONFIG_REQUIRED', 'Explicit finite detector parameters required')
    require(m['calibration_status'] in ('PUBLIC CALIBRATION GAP', 'SYNTHETIC_ONLY'), 'MODEL_CONTRACT', 'Unsupported calibration claim')


class LocalModels:
    """Only the existing YuNet + AuraFace route; all detector parameters explicit."""
    def __init__(self, detector_path, embedding_path, *, detector_sha256, embedding_sha256,
                 confidence, nms, top_k):
        self.detector_path, self.embedding_path = Path(detector_path), Path(embedding_path)
        self.manifest = {'kind': 'local-model', 'detector_sha256': detector_sha256,
                         'embedding_sha256': embedding_sha256, 'embedding_model': 'auraface-glintr100',
                         'embedding_dim': 512, 'detector_config': {'confidence': confidence, 'nms': nms, 'top_k': top_k},
                         'preprocess_version': 'auraface-convention-unvalidated-v1',
                         'calibration_status': 'PUBLIC CALIBRATION GAP'}
        validate_manifest(self.manifest)
        self.verify()
        from ..detect import YuNetDetector
        from ..embed import AuraFaceEmbedder
        self.detector = YuNetDetector(str(self.detector_path), confidence, nms, top_k)
        self.embedder = AuraFaceEmbedder(self.embedding_path)

    def verify(self):
        require(file_sha(self.detector_path) == self.manifest['detector_sha256'] and
                file_sha(self.embedding_path) == self.manifest['embedding_sha256'],
                'MODEL_CHANGED', 'Local model bytes differ')

    def detect(self, image):
        self.verify()
        return self.detector.detect(image)

    def embed(self, image, row):
        self.verify()
        return self.embedder.embed(self.embedder.align_crop(image, row))
