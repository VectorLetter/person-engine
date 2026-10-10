"""Explicit TEST/ACCEPTANCE ONLY provider; no weights, randomness or real inference.

Only the public 96x96 uniform grayscale fixture alphabet is accepted. Changing
this versioned recipe requires new manifest identifiers, not a silent fallback.
"""
import hashlib

import numpy as np

from ..public_v0.store import require


class SyntheticAcceptanceProvider:
    """Fixed geometry and orthogonal anonymous vectors; never identity authority."""

    def __init__(self):
        self.manifest = {
            'kind': 'synthetic-fixture',
            'detector_sha256': hashlib.sha256(b'synthetic-acceptance-detector/v1').hexdigest(),
            'embedding_sha256': hashlib.sha256(b'synthetic-acceptance-embedding/v1').hexdigest(),
            'embedding_model': 'synthetic-acceptance-vectors-v1', 'embedding_dim': 3,
            'detector_config': {'confidence': 0.5, 'nms': 0.3, 'top_k': 1},
            'preprocess_version': 'synthetic-acceptance-pixels-v1',
            'calibration_status': 'SYNTHETIC_ONLY',
        }

    @staticmethod
    def fixture_value(image):
        require(isinstance(image, np.ndarray) and image.shape == (96, 96, 3)
                and image.dtype == np.uint8, 'DETECTION_INVALID', 'Synthetic fixture shape required')
        value = int(image[0, 0, 0])
        require(value in (32, 64, 160, 192, 224) and np.all(image == value),
                'DETECTION_INVALID', 'Synthetic fixture pixels required')
        return value

    def detect(self, image):
        value = self.fixture_value(image)
        if value == 224:
            return []
        return [np.array([2, 2, 30, 30, 8, 10, 25, 10, 16, 17, 10, 25, 23, 25, 0.9], dtype=np.float64)]

    def embed(self, image, row):
        value = self.fixture_value(image)
        require(value != 224, 'EMBEDDING_INVALID', 'No-face fixture cannot be embedded')
        return (1., 0., 0.) if value in (32, 64) else (0., 1., 0.) if value == 160 else (0., 0., 1.)
