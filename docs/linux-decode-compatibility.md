# JPEG/PNG decode portability boundary

JPEG/PNG use bounded portable decoding on native and Docker runtimes. The `/v1`
registration, identity, model, review/apply and snapshot contracts are unchanged.

## Decode chain

The HTTP adapter accepts a path relative to the selected workspace's import
directory. Registration verifies the declared size, SHA-256 and JPEG/PNG
signature, then copies immutable bytes into content storage under the digest
without an extension. Ingest checks that content, calls the decode adapter,
then supplies the resulting array to the unchanged detector and embedder.

The adapter reuses `scan.sniff_magic` for routing. JPEG/PNG use a bounded
read, generic header validation and the existing OpenCV `IMREAD_COLOR` decoder
via `imdecode`. This path launches no subprocess and does not depend on PATH,
file extensions, `sips`, Pillow, or a new Python distribution. A second check
of the actual bytes inside the utility prevents extension-based routing and
enforces the bound even if the file size changes after the initial stat.

## Frozen JPEG/PNG array contract

| Property | Contract |
| --- | --- |
| Input | Registered JPEG/PNG bytes; extension may be wrong or absent |
| Output | `numpy.ndarray`, `uint8`, shape `(height, width, 3)`, BGR |
| JPEG orientation | Apply embedded EXIF orientation using OpenCV's `IMREAD_COLOR`; test orientations 1–8 |
| PNG alpha | Discard alpha, retain stored color samples; no background compositing |
| Grayscale | Expand to three equal BGR channels |
| 16-bit PNG | Preserve OpenCV's existing conversion to 8-bit color; grayscale test verifies high-byte conversion |
| Profiles/gamma | No new color management or normalization beyond the frozen OpenCV decoder |
| Source bound | At most 64 MiB, including the actual bounded read |
| Raster bound | At most 24,000,000 pixels, checked before OpenCV allocation and after decode |
| Unsupported magic | Explicit internal `UNSUPPORTED_MEDIA` |
| Malformed input | Invalid structure, truncated JPEG/PNG, PNG CRC failure, or OpenCV failure gives `DECODE_FAILED` |
| Limit failure | `CONTENT_LIMIT` |

JPEG preflight validates marker/segment bounds, a single frame size, scan and
end markers, including progressive multi-scan structure. It is not a full
entropy validator: damaged entropy that OpenCV successfully decodes is not
guaranteed to be rejected. PNG preflight checks bounded chunk lengths, CRCs,
IHDR dimensions and IDAT/IEND presence. Both permit decoder-compatible
trailing bytes after the end marker. No general media framework is added.

The stricter rejection of truncated files or corrupt PNG chunks is an
intentional, recorded decoder change: the previous external header probe and
OpenCV combination could tolerate some partial files. It does not alter the
registration transaction; registration still validates bytes/digest/type,
and an ingest failure still follows the existing sanitized job error mapping.
There are no added HTTP error codes or response fields.

For fixed valid anonymous fixtures, compare encoded bytes, dimensions, dtype
and BGR byte hashes before/after and on Linux. Exact pixel hashes are evidence
for those fixtures, not a promise that every JPEG entropy stream is bit-exact
across all CPU implementations or future codec versions. Dependencies remain
frozen. Any future decoder/codec change needs its own drift evidence.

## HEIC boundary

Public V0 registration continues to accept only JPEG/PNG on every platform.
The shared non-Public-V0 decoder retains the macOS `sips` HEIC route, now
selected by known HEIC magic rather than extension. Both its dimensions and
conversion use `sips`. That branch is separate from normal JPEG/PNG decoding.
On Linux, recognized HEIC raises internal `HEIC_PROVIDER_REQUIRED` without
starting a subprocess. No libheif, pillow-heif, new HEIC stack or real-photo
test is introduced. Keeping the native branch is not new HEIC acceptance
evidence and does not extend the HTTP media contract.

## Import path boundary

Native and Docker modes keep the same relative path contract. The API
does not accept host absolute paths or `/media/photos/example.jpg`. Create a
workspace and use its returned `source_directory` as an opaque placement hint
relative to the configured service root; do not reconstruct a slot name.

The supplied Docker/Compose runtime:

1. Mount the selected host photos directory at `/media/photos` read-only.
2. Mount a dedicated writable workspace volume at `/data/person-engine`,
   with the service root `/data/person-engine/service`.
3. Use `python -m person_engine.import_tool` in the container to copy approved
   source bytes into the returned `source_directory`, without modifying originals.
   See the exact helper commands and receipts in the [Docker guide](docker-runtime.md).
4. Register `source_path: "example.jpg"`, plus the existing digest/size/type
   and logical source fields. Do not prepend `imports/` unless a real nested
   directory with that name was deliberately created inside the import root.

The explicit import helper is available; there is no upload endpoint or automatic discovery.
Symlinks, hardlinks, absolute paths and traversal retain the existing rejection
behavior. Model choice and synthetic opt-in are unchanged.

## Validation and Linux runtime boundary

Run the new standalone decode tests with a report outside the repository:

```sh
python -B scripts/test_image_decode.py --report /tmp/decode-tests.json
```

The suite empties PATH and rejects subprocess calls around decode. It covers
JPEG/PNG, wrong/no extension, corruption, unsupported bytes, alpha, grayscale,
16-bit PNG, all EXIF orientations, progressive JPEG, pre-allocation limits,
Linux HEIC rejection and the synthetic provider's actual input.

The [README](../README.md) documents the core/API tests and full Docker synthetic
acceptance, including the independent HTTP consumer and container lifecycle.
The frozen non-headless OpenCV wheel requires Debian Bookworm runtime packages
`libgl1` and `libglib2.0-0` with their dependencies, installed by the Dockerfile.
Python dependency pins remain unchanged.

Linux arm64 is the verified Docker runtime. Linux amd64 remains **METADATA_ONLY**,
not runtime-verified. Wheel/base-index availability is not runtime evidence.
Real-model calibration, provenance, localhost and in-memory-job limits remain;
see [current limitations](public-v0-limitations.md).
