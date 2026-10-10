"""Anonymous decode portability tests; no models, photos or private fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import cv2
import numpy as np
from numpy.testing import assert_array_equal

from person_engine.core_io import FoundationError
from person_engine.ingest_cache import decode, LIMITS
from person_engine.api.synthetic_acceptance import SyntheticAcceptanceProvider


def fixture_bytes():
    image = np.zeros((18, 30, 3), dtype=np.uint8)
    image[:, :10] = (240, 20, 5)
    image[:, 10:20] = (10, 230, 40)
    image[:, 20:] = (30, 15, 220)
    image[3:7, 4:11] = (170, 90, 130)
    def encode(ext, value, params=()):
        ok, data = cv2.imencode(ext, value, list(params))
        assert ok
        return data.tobytes()
    jpeg = encode('.jpg', image)
    fixtures = {'color.jpg': jpeg, 'color.png': encode('.png', image),
                'progressive.jpg': encode('.jpg', image, (cv2.IMWRITE_JPEG_PROGRESSIVE, 1)),
                'alpha.png': encode('.png', np.dstack((image, np.zeros(image.shape[:2], np.uint8)))),
                'gray.png': encode('.png', np.arange(18 * 30, dtype=np.uint16).reshape(18, 30).astype(np.uint8)),
                'gray16.png': encode('.png', np.arange(18 * 30, dtype=np.uint16).reshape(18, 30) * 100),
                'synthetic.png': encode('.png', np.full((96, 96, 3), 32, np.uint8)),
                'synthetic.jpg': encode('.jpg', np.full((96, 96, 3), 32, np.uint8))}
    for orientation in range(1, 9):
        tiff = b'II' + struct.pack('<HIH', 42, 8, 1)
        tiff += struct.pack('<HHI', 0x112, 3, 1) + struct.pack('<H', orientation) + b'\0\0' + b'\0' * 4
        exif = b'Exif\0\0' + tiff
        fixtures[f'orientation-{orientation}.jpg'] = jpeg[:2] + b'\xff\xe1' + struct.pack('>H', len(exif) + 2) + exif + jpeg[2:]
    return fixtures


def signatures(fixtures, directory):
    result = {}
    for name, data in fixtures.items():
        path = Path(directory) / name
        path.write_bytes(data)
        image = decode(path, directory)
        result[name] = {'encoded_sha256': hashlib.sha256(data).hexdigest(),
                        'shape': list(image.shape), 'dtype': str(image.dtype),
                        'bgr_sha256': hashlib.sha256(image.tobytes()).hexdigest()}
    return result


FIXTURES = None


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='anonymous_decode_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixtures = FIXTURES or fixture_bytes()
        # Every test runs without a sips executable and rejects subprocess use.
        self.no_path = patch.dict(os.environ, {'PATH': str(self.root / 'empty-path')})
        self.no_path.start()
        self.addCleanup(self.no_path.stop)
        self.no_process = patch('subprocess.run', side_effect=AssertionError('JPEG/PNG launched subprocess'))
        self.no_process.start()
        self.addCleanup(self.no_process.stop)

    def decode_bytes(self, data, name='content'):
        path = self.root / name
        path.write_bytes(data)
        return decode(path, self.root)

    def rejects(self, data, code='DECODE_FAILED'):
        for _ in range(2):
            with self.assertRaises(FoundationError) as caught:
                self.decode_bytes(data)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(str(self.root), str(caught.exception))

    def test_jpeg(self):
        image = self.decode_bytes(self.fixtures['color.jpg'])
        self.assertEqual(image.shape, (18, 30, 3))
        self.assertEqual(image.dtype, np.uint8)
        self.assertGreater(int(image[0, 0, 0]), int(image[0, 0, 2]))

    def test_png(self):
        image = self.decode_bytes(self.fixtures['color.png'])
        self.assertEqual(image.shape, (18, 30, 3))
        self.assertEqual(image[0, 0].tolist(), [240, 20, 5])
        self.assertEqual(image.dtype, np.uint8)

    def test_jpeg_wrong_extension(self):
        a = self.decode_bytes(self.fixtures['color.jpg'], 'photo.heic')
        b = self.decode_bytes(self.fixtures['color.jpg'], 'photo.png')
        assert_array_equal(a, b)

    def test_png_wrong_extension(self):
        a = self.decode_bytes(self.fixtures['color.png'], 'photo.heif')
        b = self.decode_bytes(self.fixtures['color.png'], 'photo.jpg')
        assert_array_equal(a, b)

    def test_no_extension(self):
        for name in ('color.jpg', 'color.png'):
            self.assertEqual(self.decode_bytes(self.fixtures[name], 'a' * 64).shape, (18, 30, 3))

    def test_no_sips_available(self):
        self.assertIsNone(shutil.which('sips'))
        for name in ('color.jpg', 'color.png'):
            self.decode_bytes(self.fixtures[name])

    def test_truncated_jpeg(self):
        self.rejects(self.fixtures['color.jpg'][:-2])

    def test_invalid_jpeg_segment(self):
        self.rejects(b'\xff\xd8\xff\xe0\x00\x01\xff\xd9')

    def test_truncated_png(self):
        self.rejects(self.fixtures['color.png'][:-12])

    def test_corrupt_png_crc(self):
        data = bytearray(self.fixtures['color.png'])
        data[29] ^= 1
        self.rejects(bytes(data))

    def test_unsupported(self):
        for data in (b'GIF89a' + b'\0' * 20, b'not an image', b''):
            self.rejects(data, 'UNSUPPORTED_MEDIA')

    def test_heic_linux_explicit(self):
        with patch('person_engine.ingest_cache.sys.platform', 'linux'):
            self.rejects(b'\0\0\0\x18ftypheic' + b'\0' * 12, 'HEIC_PROVIDER_REQUIRED')

    def test_alpha_dropped_not_composited(self):
        assert_array_equal(self.decode_bytes(self.fixtures['alpha.png']),
                                      self.decode_bytes(self.fixtures['color.png']))

    def test_grayscale_expanded(self):
        image = self.decode_bytes(self.fixtures['gray.png'])
        self.assertEqual(image.shape, (18, 30, 3))
        assert_array_equal(image[:, :, 0], image[:, :, 1])
        assert_array_equal(image[:, :, 1], image[:, :, 2])

    def test_png_16bit_to_8bit(self):
        image = self.decode_bytes(self.fixtures['gray16.png'])
        self.assertEqual(image.dtype, np.uint8)
        expected = (np.arange(18 * 30, dtype=np.uint16).reshape(18, 30) * 100 >> 8).astype(np.uint8)
        assert_array_equal(image[:, :, 0], expected)

    def test_exif_all_orientations(self):
        base = self.decode_bytes(self.fixtures['color.jpg'])
        expected = [base, cv2.flip(base, 1), cv2.flip(base, -1), cv2.flip(base, 0),
                    cv2.transpose(base), cv2.rotate(base, cv2.ROTATE_90_CLOCKWISE),
                    cv2.flip(cv2.transpose(base), -1), cv2.rotate(base, cv2.ROTATE_90_COUNTERCLOCKWISE)]
        for number, image in enumerate(expected, 1):
            assert_array_equal(self.decode_bytes(self.fixtures[f'orientation-{number}.jpg']), image)

    def test_progressive_jpeg(self):
        self.assertEqual(self.decode_bytes(self.fixtures['progressive.jpg']).shape, (18, 30, 3))

    def test_png_pixel_limit_before_opencv(self):
        data = bytearray(self.fixtures['color.png'])
        data[16:24] = struct.pack('>II', 24_000_001, 1)
        data[29:33] = struct.pack('>I', zlib.crc32(data[12:29]))
        with patch('cv2.imdecode', side_effect=AssertionError('unbounded allocation')):
            self.rejects(bytes(data), 'CONTENT_LIMIT')

    def test_jpeg_pixel_limit_before_opencv(self):
        data = bytearray(self.fixtures['color.jpg'])
        pos = data.index(b'\xff\xc0')
        data[pos + 5:pos + 9] = struct.pack('>HH', 65535, 65535)
        with patch('cv2.imdecode', side_effect=AssertionError('unbounded allocation')):
            self.rejects(bytes(data), 'CONTENT_LIMIT')

    def test_byte_limit(self):
        with patch.dict(LIMITS, {'max_source_bytes': 20}):
            self.rejects(self.fixtures['color.png'], 'CONTENT_LIMIT')

    def test_downstream_provider_input(self):
        for name in ('synthetic.jpg', 'synthetic.png'):
            image = self.decode_bytes(self.fixtures[name])
            self.assertEqual(image.shape, (96, 96, 3))
            provider = SyntheticAcceptanceProvider()
            rows = provider.detect(image)
            self.assertEqual(len(rows), 1)
            self.assertEqual(provider.embed(image, rows[0]), (1., 0., 0.))

    def test_decoder_failure_sanitized(self):
        with patch('cv2.imdecode', return_value=None):
            self.rejects(self.fixtures['color.jpg'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--fixture-dir', type=Path)
    args = parser.parse_args()
    FIXTURES = ({p.name: p.read_bytes() for p in sorted(args.fixture_dir.iterdir())}
                if args.fixture_dir else fixture_bytes())
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DecodeTests))
    with tempfile.TemporaryDirectory(prefix='anonymous_decode_signatures_') as directory:
        with patch.dict(os.environ, {'PATH': directory}), patch('subprocess.run', side_effect=AssertionError('subprocess')):
            matrices = signatures(FIXTURES, directory)
    report = {'status': 'PASS' if result.wasSuccessful() else 'FAIL', 'tests_run': result.testsRun,
              'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
              'signatures': matrices, 'opencv_version': cv2.__version__, 'no_sips': True}
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    raise SystemExit(not result.wasSuccessful())
