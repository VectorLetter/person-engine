"""Stage 4A — Scan Foundation: manifest reader, content hash,
magic-byte sniff, durable index, manifest diff.

Design principles:
- streaming manifest reader (no 50k rows in memory at once)
- content_sha256 computed via streaming hash (never reads whole file into one buffer)
- readable file always gets a hash; only I/O error yields null
- magic-byte sniff fixes HEIC-bytes-with-JPG-extension decode routing
- HEIC detection restricted to known brands (not any ftyp)
- durable index keyed by asset_id (NOT content_sha256)
- manifest diff classifies into: new/unchanged/moved/revised/distinct_same_content/unavailable

Availability vs Inference State (Stage 4A.4 contract):
- IndexEntry persists two INDEPENDENT fields:
  * availability:      "available" | "unavailable"
  * inference_status:  "pending" | "complete" | "failed"
- "unavailable" describes CURRENT file access only. It NEVER implies the
  inference itself failed: a missing external disk that reappears later
  preserves its prior inference_status.
- The old combined `status` string is kept ONLY as a backward-compatible
  reader input; new writes always use the two explicit fields.
- Stage 4B staging consumes logical occurrences via AssetFaceOccurrence
  (asset_id, face_id), NOT raw content identity.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# ── Constants ──────────────────────────────────────────────────────────

MANIFEST_SCHEMA = "asset_manifest/1.0"
CHUNK_SIZE = 1 << 20  # 1 MiB streaming read

# Magic bytes
JPEG_MAGIC = b'\xff\xd8\xff'
# Standard PNG signature: \x89 'P' 'N' 'G' \r \n \x1a \n
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

# Known HEIF brands (specific, not generic 'ftyp')
# Only these exact 4-byte brand strings map to "heic" format
KNOWN_HEIC_BRANDS = frozenset([
    'heic', 'heix', 'hevc', 'hevx',
    'mif1', 'msf1',
    'heis', 'hevm', 'hevs',
])


# ── Content hash ────────────────────────────────────────────────────────

def compute_content_sha256(path: str | Path) -> str | None:
    """Stream-compute SHA-256 of file content.

    Returns hex digest for readable files.
    Returns None ONLY when the file cannot be read (I/O error / permission).
    A file that can be read but fails to decode STILL gets a hash.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError, FileNotFoundError):
        return None


# ── Magic-byte sniff ────────────────────────────────────────────────────

@dataclass
class SniffResult:
    format: str          # "jpeg" | "heic" | "png" | "unknown"
    actual_ext: str      # ".jpg", ".heic", etc.
    mismatch: bool       # True if magic byte disagrees with extension


def _extract_brand(header: bytes) -> str | None:
    """Extract HEIF brand from ftyp box header.

    Returns the brand string (e.g. 'heic', 'mif1') or None if not a known brand.
    Only recognizes specific HEIF brands, NOT generic ftyp.
    """
    if len(header) < 12:
        return None
    box_type = header[4:8]
    if box_type != b'ftyp':
        return None
    brand = header[8:12]
    try:
        brand_str = brand.decode('ascii')
    except UnicodeDecodeError:
        return None
    if brand_str in KNOWN_HEIC_BRANDS:
        return brand_str
    return None


def sniff_magic(path: str | Path) -> SniffResult:
    """Sniff first bytes to determine actual image format.

    Fixes the HEIC-bytes-but-JPG-extension bug:
    a .JPG file with HEIC magic bytes should be decoded via sips (HEIC path),
    not cv2.imread (which fails on HEIC data).

    Brand filtering: only specific HEIF brands (heic/heix/hevc/hevx/mif1/msf1)
    are recognized as HEIC. Other ftyp brands (avif, qt, etc.) return 'unknown'.
    """
    actual_ext = Path(path).suffix.lower()
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except (OSError, PermissionError, FileNotFoundError):
        return SniffResult(format="unknown", actual_ext=actual_ext, mismatch=False)

    if not header:
        return SniffResult(format="unknown", actual_ext=actual_ext, mismatch=False)

    # JPEG: \xff\xd8\xff
    if header.startswith(JPEG_MAGIC):
        fmt = "jpeg"
    # Check for specific HEIF brand (not generic ftyp)
    elif len(header) >= 12 and header[4:8] == b'ftyp':
        brand = header[8:12]
        try:
            brand_str = brand.decode('ascii')
        except UnicodeDecodeError:
            brand_str = ""
        if brand_str in KNOWN_HEIC_BRANDS:
            fmt = "heic"
        else:
            fmt = "unknown"
    # PNG
    elif header.startswith(PNG_MAGIC):
        fmt = "png"
    else:
        fmt = "unknown"

    mismatch = False
    if fmt == "heic" and actual_ext in (".jpg", ".jpeg"):
        mismatch = True
    elif fmt == "jpeg" and actual_ext in (".heic", ".heif"):
        mismatch = True

    return SniffResult(format=fmt, actual_ext=actual_ext, mismatch=mismatch)


# ── Manifest reader (streaming) ────────────────────────────────────────

@dataclass
class ManifestEntry:
    schema: str
    asset_id: str
    path: str
    content_sha256: str | None = None
    mtime: str | None = None
    exif_date: str | None = None
    width: int | None = None
    height: int | None = None
    file_size: int | None = None
    provenance: str | None = None
    tags: list[str] = field(default_factory=list)
    line_number: int = 0


class ManifestReadError(Exception):
    pass


class ManifestSchemaError(ManifestReadError):
    pass


class DuplicateAssetIdError(ManifestReadError):
    pass


def read_manifest(path: str | Path) -> Iterator[ManifestEntry]:
    """Streaming reader for asset_manifest.jsonl.

    Yields ManifestEntry objects one at a time.
    Raises on: invalid JSON, missing schema, unknown schema version,
    duplicate asset_id, missing required fields.
    """
    path = Path(path)
    if not path.exists():
        raise ManifestReadError(f"manifest not found: {path}")

    seen_asset_ids: set[str] = set()

    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ManifestReadError(
                    f"line {line_no}: invalid JSON: {e}") from e

            schema = obj.get("schema")
            if schema is None:
                raise ManifestSchemaError(
                    f"line {line_no}: missing 'schema' field")
            if schema != MANIFEST_SCHEMA:
                raise ManifestSchemaError(
                    f"line {line_no}: unsupported schema '{schema}' "
                    f"(expected '{MANIFEST_SCHEMA}')")

            asset_id = obj.get("asset_id")
            if not asset_id:
                raise ManifestReadError(
                    f"line {line_no}: missing required 'asset_id'")

            file_path = obj.get("path")
            if not file_path:
                raise ManifestReadError(
                    f"line {line_no}: missing required 'path'")

            if asset_id in seen_asset_ids:
                raise DuplicateAssetIdError(
                    f"line {line_no}: duplicate asset_id '{asset_id}'")
            seen_asset_ids.add(asset_id)

            yield ManifestEntry(
                schema=schema,
                asset_id=asset_id,
                path=file_path,
                content_sha256=obj.get("content_sha256"),
                mtime=obj.get("mtime"),
                exif_date=obj.get("exif_date"),
                width=obj.get("width"),
                height=obj.get("height"),
                file_size=obj.get("file_size"),
                provenance=obj.get("provenance"),
                tags=obj.get("tags", []),
                line_number=line_no,
            )


# ── Durable index ───────────────────────────────────────────────────────

# Valid values
AVAILABILITIES = frozenset({"available", "unavailable"})
INFERENCE_STATUSES = frozenset({"pending", "complete", "failed"})

# Old combined status -> (availability, inference_status) migration map.
# "failed" is a legacy inference failure recorded while the file was readable
# -> availability stays "available", inference becomes "failed".
# NOTE: "unavailable" alone must NOT be assumed to mean "inference failed";
# if inference state cannot be determined for an unavailable legacy entry it
# maps to "pending", never "failed" (unavailable != inference failed).
_LEGACY_STATUS_MAP = {
    "new":             ("available", "pending"),
    "pending":         ("available", "pending"),
    "pending_inference": ("available", "pending"),
    "moved":          ("available", "pending"),
    "revised":        ("available", "pending"),
    "distinct_same_content": ("available", "pending"),
    "processed":      ("available", "complete"),
    "complete":       ("available", "complete"),
    "failed":         ("available", "failed"),
    "unavailable":    ("unavailable", "pending"),  # inference state unknown
}


@dataclass
class IndexEntry:
    asset_id: str
    content_sha256: str | None
    current_path: str
    # Persisted, independent state fields (Stage 4A.4 contract):
    availability: str = "available"      # available | unavailable
    inference_status: str = "pending"    # pending | complete | failed
    # Backward-compat combined status (reader-only; no longer the single source
    # of truth for new writes). Old indexes may carry only this field.
    status: str | None = None
    processed_content_version: str | None = None
    last_seen_run: str | None = None

    def __post_init__(self):
        if self.availability not in AVAILABILITIES:
            raise ValueError(f"invalid availability: {self.availability!r}")
        if self.inference_status not in INFERENCE_STATUSES:
            raise ValueError(
                f"invalid inference_status: {self.inference_status!r}")

    @classmethod
    def from_legacy_status(cls, status: str, **kw) -> "IndexEntry":
        """Build an entry from an old combined status string.

        Migration rules (see _LEGACY_STATUS_MAP):
        - failed      -> availability=available,   inference_status=failed
        - unavailable -> availability=unavailable, inference_status=pending
                        (inference state unknown; NOT assumed to have failed)
        - processed   -> availability=available,   inference_status=complete
        """
        avail, infer = _LEGACY_STATUS_MAP.get(status, ("available", "pending"))
        return cls(availability=avail, inference_status=infer, **kw)

    def legacy_status(self) -> str:
        """Best-effort combined status string for backward-compat readers."""
        if self.availability == "unavailable":
            return "unavailable"
        if self.inference_status == "complete":
            return "processed"
        if self.inference_status == "failed":
            return "failed"
        return "pending_inference"


def load_index(index_path: str | Path) -> dict[str, IndexEntry]:
    """Load durable asset index from JSONL file.

    Keyed by asset_id (logical asset identity).
    content_sha256 is cache/content identity, NOT the primary key.

    Backward-compatible:
    - New entries may carry explicit availability + inference_status fields.
    - Old entries carry only a combined `status`; they are migrated via
      IndexEntry.from_legacy_status() (failed -> available+failed,
      unavailable -> unavailable+pending, processed -> available+complete).
    """
    index_path = Path(index_path)
    result: dict[str, IndexEntry] = {}
    if not index_path.exists():
        return result
    with open(index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            asset_id = obj["asset_id"]
            base = dict(
                asset_id=asset_id,
                content_sha256=obj.get("content_sha256"),
                current_path=obj.get("current_path", ""),
                processed_content_version=obj.get("processed_content_version"),
                last_seen_run=obj.get("last_seen_run"),
            )
            avail = obj.get("availability")
            infer = obj.get("inference_status")
            if avail is not None and infer is not None:
                # New format: explicit fields (validated by setters)
                entry = IndexEntry(
                    availability=avail, inference_status=infer, **base)
            elif "status" in obj and obj["status"] is not None:
                # Old format: migrate combined status
                entry = IndexEntry.from_legacy_status(
                    obj["status"],
                    asset_id=asset_id,
                    content_sha256=base["content_sha256"],
                    current_path=base["current_path"],
                    processed_content_version=base["processed_content_version"],
                    last_seen_run=base["last_seen_run"],
                )
            else:
                entry = IndexEntry(
                    availability="available", inference_status="pending", **base)
            result[asset_id] = entry
    return result


def save_index(index_path: str | Path, index: dict[str, IndexEntry]) -> None:
    """Atomically write index to JSONL using the explicit field contract."""
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in index.values():
            f.write(json.dumps({
                "asset_id": entry.asset_id,
                "content_sha256": entry.content_sha256,
                "current_path": entry.current_path,
                "availability": entry.availability,
                "inference_status": entry.inference_status,
                "processed_content_version": entry.processed_content_version,
                "last_seen_run": entry.last_seen_run,
            }, ensure_ascii=False) + "\n")
    os.replace(tmp, index_path)


# ── Logical occurrence contract (Stage 4B staging input) ──────────────
#
# content-level compute is keyed by face_id = hash(content + detector + face_idx)
# so exact-duplicate assets MAY share detect result / embedding / face_id.
# But Stage 4B staging MUST emit logical occurrences, one row per
# (asset_id, face_id), so distinct_same_content keeps two rows.

@dataclass
class AssetFaceOccurrence:
    asset_id: str
    face_id: str
    content_sha256: str | None
    face_idx: int | None = None

    def key(self) -> tuple[str, str]:
        """Logical occurrence identity: (asset_id, face_id)."""
        return (self.asset_id, self.face_id)



# ── Manifest diff ───────────────────────────────────────────────────────

@dataclass
class DiffEntry:
    asset_id: str
    category: str  # new | unchanged | moved | revised | distinct_same_content | unavailable
    path: str
    content_sha256: str | None = None
    previous_path: str | None = None
    previous_content_sha256: str | None = None
    reason: str = ""


def compute_manifest_diff(
    manifest_entries: list[ManifestEntry],
    existing_index: dict[str, IndexEntry],
) -> list[DiffEntry]:
    """Compute manifest diff against existing durable index.

    Categories:
    - new: asset_id not in index
    - unchanged: same asset_id + same content_sha256 + same path
    - moved: same asset_id + same content_sha256 + path changed
      (does NOT reset inference state — preserves pending/complete)
    - revised: same asset_id + content_sha256 changed
    - distinct_same_content: different asset_id + same content_sha256 as another asset
    - unavailable: file not found OR hash read failed AND no manifest hash
    """
    content_to_assets: dict[str, list[str]] = {}
    diffs: list[DiffEntry] = []

    for entry in manifest_entries:
        # Check file availability and compute hash
        file_exists = Path(entry.path).exists()

        if not file_exists:
            diffs.append(DiffEntry(
                asset_id=entry.asset_id,
                category="unavailable",
                path=entry.path,
                content_sha256=entry.content_sha256,
                reason="file not found",
            ))
            continue

        # Compute hash if not provided
        content_sha = entry.content_sha256
        hash_failed = False
        if content_sha is None:
            content_sha = compute_content_sha256(entry.path)
            if content_sha is None:
                hash_failed = True

        # Classify as unavailable if hash read failed AND no manifest hash
        if hash_failed:
            diffs.append(DiffEntry(
                asset_id=entry.asset_id,
                category="unavailable",
                path=entry.path,
                content_sha256=None,
                reason="hash computation failed and no manifest hash provided",
            ))
            continue

        # Track for distinct_same_content
        if content_sha is not None:
            content_to_assets.setdefault(content_sha, []).append(entry.asset_id)

        existing = existing_index.get(entry.asset_id)

        if existing is None:
            diffs.append(DiffEntry(
                asset_id=entry.asset_id,
                category="new",
                path=entry.path,
                content_sha256=content_sha,
                reason="asset_id not in index",
            ))
        else:
            path_changed = existing.current_path != entry.path
            hash_changed = (existing.content_sha256 != content_sha
                            if content_sha is not None else False)

            if hash_changed:
                diffs.append(DiffEntry(
                    asset_id=entry.asset_id,
                    category="revised",
                    path=entry.path,
                    content_sha256=content_sha,
                    previous_path=existing.current_path,
                    previous_content_sha256=existing.content_sha256,
                    reason="content_sha256 changed",
                ))
            elif path_changed:
                diffs.append(DiffEntry(
                    asset_id=entry.asset_id,
                    category="moved",
                    path=entry.path,
                    content_sha256=content_sha,
                    previous_path=existing.current_path,
                    previous_content_sha256=existing.content_sha256,
                    reason="path changed, content unchanged",
                ))
            else:
                diffs.append(DiffEntry(
                    asset_id=entry.asset_id,
                    category="unchanged",
                    path=entry.path,
                    content_sha256=content_sha,
                    previous_path=existing.current_path,
                    previous_content_sha256=existing.content_sha256,
                    reason="no change",
                ))

    # Mark distinct_same_content
    for content_sha, asset_ids in content_to_assets.items():
        if len(asset_ids) > 1:
            for aid in asset_ids:
                for d in diffs:
                    if d.asset_id == aid and d.category == "new":
                        d.category = "distinct_same_content"
                        d.reason = (
                            f"same content_sha256 as {len(asset_ids)} "
                            f"distinct asset_ids")

    return diffs


def diff_summary(diffs: list[DiffEntry]) -> dict:
    """Produce a summary dict for manifest_diff.json output."""
    from collections import Counter
    counts = Counter(d.category for d in diffs)
    return {
        "total_assets": len(diffs),
        "classified_assets": len(diffs),
        "new_assets": counts.get("new", 0),
        "revised_assets": counts.get("revised", 0),
        "moved_assets": counts.get("moved", 0),
        "unchanged_assets": counts.get("unchanged", 0),
        "distinct_same_content_assets": counts.get("distinct_same_content", 0),
        "unavailable_assets": counts.get("unavailable", 0),
        "categories": dict(counts),
    }
