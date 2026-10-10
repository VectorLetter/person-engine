"""Bounded JPEG/PNG preflight for the existing OpenCV color decoder.

No filename routing, platform codec, model code or new dependency. Header
validation bounds raster allocation; it is not a full JPEG entropy validator.
"""
import struct
import zlib

import cv2
import numpy as np

from .core_io import FoundationError
from .scan import JPEG_MAGIC, PNG_MAGIC


def _require(ok, code='DECODE_FAILED'):
    if not ok:
        raise FoundationError(code, 'Invalid or unsupported image data')


def _bounded(width, height, max_pixels):
    _require(width > 0 and height > 0)
    _require(width * height <= max_pixels, 'CONTENT_LIMIT')
    return width, height


def _png_size(data, max_pixels):
    pos, size, seen_data = 8, None, False
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], 'big')
        kind = data[pos + 4:pos + 8]
        end = pos + 8 + length
        _require(end + 4 <= len(data))
        _require(zlib.crc32(memoryview(data)[pos + 4:end]) ==
                 int.from_bytes(data[end:end + 4], 'big'))
        if size is None:
            _require(kind == b'IHDR' and length == 13)
            size = _bounded(*struct.unpack('>II', data[pos + 8:pos + 16]), max_pixels)
        else:
            _require(kind != b'IHDR')
        if kind == b'IDAT':
            seen_data = True
        if kind == b'IEND':
            _require(length == 0 and seen_data)
            return size
        pos = end + 4
    _require(False)


def _jpeg_size(data, max_pixels):
    pos, size, entropy, seen_scan = 2, None, False, False
    frame_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while pos < len(data):
        if entropy:
            pos = data.find(b'\xff', pos)
            _require(pos >= 0)
        _require(data[pos] == 0xFF)
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        _require(pos < len(data))
        marker = data[pos]
        pos += 1
        if entropy and (marker == 0 or 0xD0 <= marker <= 0xD7):
            continue
        entropy = False
        if marker == 0xD9:
            _require(size is not None and seen_scan)
            return size
        _require(marker not in (0, 0xD8) and not 0xD0 <= marker <= 0xD7)
        if marker == 0x01:
            continue
        _require(pos + 2 <= len(data))
        length = int.from_bytes(data[pos:pos + 2], 'big')
        _require(length >= 2 and pos + length <= len(data))
        if marker in frame_markers:
            _require(size is None and length >= 8)
            height, width = struct.unpack('>HH', data[pos + 3:pos + 7])
            _require(length == 8 + 3 * data[pos + 7])
            size = _bounded(width, height, max_pixels)
        if marker == 0xDA:
            _require(size is not None and length >= 6)
            seen_scan, entropy = True, True
        pos += length
    _require(False)


def decode_jpeg_png(path, *, max_bytes, max_pixels):
    """Return H x W x 3 uint8 BGR; OpenCV applies EXIF and drops alpha.

    PNG 16-bit samples retain OpenCV IMREAD_COLOR's 8-bit conversion. No
    compositing or color-profile conversion is added. Invalid structures and
    decoder failures raise DECODE_FAILED; limits raise CONTENT_LIMIT.
    """
    with open(path, 'rb') as source:
        data = source.read(max_bytes + 1)
    _require(len(data) <= max_bytes, 'CONTENT_LIMIT')
    if data.startswith(JPEG_MAGIC):
        size = _jpeg_size(data, max_pixels)
    elif data.startswith(PNG_MAGIC):
        size = _png_size(data, max_pixels)
    else:
        _require(False, 'UNSUPPORTED_MEDIA')
    try:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        raise FoundationError('DECODE_FAILED', 'Image decoder rejected data') from None
    _require(image is not None and image.dtype == np.uint8 and
             image.ndim == 3 and image.shape[2] == 3)
    _bounded(image.shape[1], image.shape[0], max_pixels)
    # EXIF orientation may transpose dimensions, but cannot change area.
    _require(image.shape[:2] in ((size[1], size[0]), (size[0], size[1])))
    return image
