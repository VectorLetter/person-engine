"""Portable stable asset registry. Root admission is explicit; no private library defaults."""
from __future__ import annotations
import errno
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from .scan import sniff_magic
MANIFEST_SCHEMA = 'asset_manifest/1.0'
IMG_EXT = frozenset({'heic', 'jpg', 'jpeg', 'png', 'bmp', 'tif', 'gif'})
VID_EXT = frozenset({'mov', 'mp4'})
SIDECAR_EXT = frozenset({'aae'})
STILL_EXT_PRIORITY: tuple[str, ...] = ('heic', 'jpg', 'jpeg', 'png', 'tif', 'bmp', 'gif')
RE_EDIT = re.compile('_E(\\d+)(\\s+\\d+)?$')
RE_ORIG = re.compile('_O(\\d+)(\\s+\\d+)?$')
VARIANT_ORIGINAL = 'original'
VARIANT_EDITED = 'edited'
VARIANT_LIVE = 'live'
VARIANT_VIDEO_EDITED = 'video_edited'
VARIANT_SIDECAR = 'sidecar'
VARIANT_VIDEO = 'video'
BASIS_BASE_RULE = 'base_rule_v1'
BASIS_SINGLE_IMPORT = 'single_import_v1'
IMPORT_SOURCE_EXT = frozenset({'jpg', 'jpeg', 'png'})
_REGISTRY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, 'photo-organizer.asset-registry.v1')

class AssetRegistryError(RuntimeError):
    pass

class DerivedRootError(AssetRegistryError):
    """A file from a derived / non-canonical root was passed to the registry."""

class GeneratedSourceError(AssetRegistryError):
    """A generated / derived source file was passed to register_file."""

def base_of(stem: str) -> str:
    """Strip edit/original markers while retaining a duplicate suffix."""
    for rx in (RE_EDIT, RE_ORIG):
        m = rx.search(stem)
        if m:
            stem = stem[:m.start()] + '_' + m.group(1) + (m.group(2) or '')
    return stem

def is_edit(stem: str) -> bool:
    return bool(RE_EDIT.search(stem))

def ext_of(rel: str | Path) -> str:
    return Path(rel).suffix.lstrip('.').lower()

def media_type_of(rel: str) -> str:
    ext = ext_of(rel)
    if ext in IMG_EXT:
        return 'image'
    if ext in VID_EXT:
        return 'video'
    if ext in SIDECAR_EXT:
        return 'sidecar'
    return 'other'

def classify_variant(rel: str, group_has_still: bool) -> str | None:
    """Kind of resource. None => not a photo resource (never minted)."""
    ext = ext_of(rel)
    stem = Path(rel).stem
    if ext in IMG_EXT:
        return VARIANT_EDITED if is_edit(stem) else VARIANT_ORIGINAL
    if ext in VID_EXT:
        if is_edit(stem):
            return VARIANT_VIDEO_EDITED
        return VARIANT_LIVE if group_has_still else VARIANT_VIDEO
    if ext in SIDECAR_EXT:
        return VARIANT_SIDECAR
    return None

def group_key(rel: str) -> tuple[str, str]:
    p = Path(rel)
    return (p.parts[0], base_of(p.stem))

def _mint_asset_id(origin_key: str) -> str:
    return 'po_' + uuid.uuid5(_REGISTRY_NAMESPACE, origin_key).hex

def _mint_occurrence_id(rel_path: str) -> str:
    return 'occ_' + uuid.uuid5(_REGISTRY_NAMESPACE, 'occ|' + rel_path).hex

def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def _looks_generated(path: Path) -> bool:
    """Fail-closed generated-source admission check (mirrors the organizer
    photomosaic backend's ``is_generated_photomosaic`` path rule): a sibling /
    adjacent photomosaic-style ``metadata.json`` (or an unreadable or symlinked
    one) marks the file as generated and it can never enter as an original."""
    for sidecar in (path.with_suffix('.metadata.json'), path.parent / 'metadata.json'):
        if sidecar.is_symlink():
            return True
        if not sidecar.exists():
            continue
        try:
            data = json.loads(sidecar.read_text())
        except (OSError, ValueError):
            return True
        if not isinstance(data, dict):
            return True
        if data.get('kind') == 'photomosaic' or data.get('generator') == 'photomosaic' or data.get('origin_kind') in ('generated', 'derived') or (data.get('excluded_from_person_engine') is True):
            return True
    return False

def _safe_unlink(path: Path) -> bool:
    """Remove a file this import created itself (never a pre-existing one)."""
    try:
        os.unlink(path)
        return True
    except OSError:
        return False

def _is_eexists(e: OSError) -> bool:
    """True when an OSError means "destination already exists".

    Normally FileExistsError/errno EEXIST; some sandboxed filesystems
    surface EEXIST as PermissionError with errno None, so the message is
    checked as a fallback. Real permission failures (EACCES) never match.
    """
    if isinstance(e, FileExistsError) or e.errno == errno.EEXIST:
        return True
    return e.errno is None and 'file already exists' in str(e)

@dataclass
class FileRecord:
    """One currently-existing library file (usually from inventory.sqlite)."""
    rel_path: str
    sha256: str
    bytes: int
    stat_signature: str | None = None

@dataclass
class ResourceAttachment:
    """Explicit owner-authorized cross-root resource attachment. No external policy is loaded."""
    rel_path: str
    target_still_path: str
    basis: str

@dataclass
class GroupMerge:
    """Explicit owner-authorized merge of same-photo groups."""
    path_a: str
    path_b: str
    basis: str

@dataclass
class UnresolvedRecord:
    item: str
    kind: str
    path: str
    reason: str
    candidates: list[str] = field(default_factory=list)

@dataclass
class BatchItem:
    batch_id: str
    legacy_key: str
    rel_paths: list[str]
    capture_date: str | None = None
    capture_source: str | None = None

@dataclass
class ReconcileReport:
    run_id: str
    files_seen: int = 0
    occurrences_unchanged: int = 0
    occurrences_revised: int = 0
    occurrences_moved: int = 0
    occurrences_new: int = 0
    occurrences_missing: int = 0
    assets_minted: int = 0
    assets_unchanged: int = 0
    assets_merged: int = 0
    attachments_applied: int = 0
    batch_links: int = 0
    unresolved: int = 0
    non_photo_files: int = 0
    notes: list[str] = field(default_factory=list)

@dataclass
class ImportResult:
    """Result of a single-file incremental import (register_file).

    ``action``:
      * "minted"        — new asset + occurrence created at ``rel_path``
      * "reused_content"— the content is already registered (exactly one live
                          asset); its ``asset_id`` is returned, nothing written
      * "reused_path"   — the destination occurrence already exists with the
                          same content (idempotent / concurrent winner)
    """
    asset_id: str
    action: str
    rel_path: str
    content_sha256: str
    bytes: int
    occurrence_id: str | None = None
    canonical_occurrence_id: str | None = None
    copied: bool = False
_SCHEMA = "\nCREATE TABLE IF NOT EXISTS media_content (\n  content_sha256 TEXT PRIMARY KEY,\n  bytes INTEGER NOT NULL,\n  media_type TEXT NOT NULL,\n  first_seen_at TEXT NOT NULL,\n  last_seen_at TEXT NOT NULL\n);\nCREATE TABLE IF NOT EXISTS resource_occurrence (\n  occurrence_id TEXT PRIMARY KEY,\n  current_path TEXT NOT NULL UNIQUE,\n  import_root TEXT NOT NULL,\n  stat_signature TEXT,\n  content_sha256 TEXT NOT NULL REFERENCES media_content(content_sha256),\n  state TEXT NOT NULL CHECK (state IN ('current','missing')),\n  first_seen_at TEXT NOT NULL,\n  last_seen_at TEXT NOT NULL,\n  moved_from TEXT\n);\nCREATE TABLE IF NOT EXISTS photo_asset (\n  asset_id TEXT PRIMARY KEY,\n  origin_key TEXT NOT NULL UNIQUE,\n  media_type TEXT NOT NULL,\n  capture_date TEXT,\n  capture_source TEXT,\n  capture_precision TEXT,\n  width INTEGER,\n  height INTEGER,\n  canonical_occurrence_id TEXT,\n  merged_into TEXT,\n  created_at TEXT NOT NULL,\n  updated_at TEXT NOT NULL\n);\nCREATE TABLE IF NOT EXISTS asset_resource (\n  asset_id TEXT NOT NULL REFERENCES photo_asset(asset_id),\n  occurrence_id TEXT NOT NULL REFERENCES resource_occurrence(occurrence_id),\n  variant TEXT NOT NULL,\n  relation_basis TEXT NOT NULL,\n  attached_at TEXT NOT NULL,\n  PRIMARY KEY (asset_id, occurrence_id)\n);\nCREATE TABLE IF NOT EXISTS batch_asset_link (\n  batch_id TEXT NOT NULL,\n  legacy_key TEXT NOT NULL,\n  asset_id TEXT REFERENCES photo_asset(asset_id),\n  mapping_status TEXT NOT NULL,\n  capture_date TEXT,\n  capture_source TEXT,\n  PRIMARY KEY (batch_id, legacy_key)\n);\nCREATE TABLE IF NOT EXISTS unresolved_attachment (\n  item TEXT PRIMARY KEY,\n  kind TEXT NOT NULL,\n  path TEXT NOT NULL,\n  reason TEXT NOT NULL,\n  candidates_json TEXT\n);\nCREATE TABLE IF NOT EXISTS registry_run (\n  run_id TEXT PRIMARY KEY,\n  started_at TEXT NOT NULL,\n  finished_at TEXT,\n  mode TEXT NOT NULL,\n  stats_json TEXT\n);\n"

class AssetRegistry:
    """Persistent registry with explicit owner-selected library roots."""

    def __init__(self, db_path: str | Path, *, root_policy):
        self.root_policy = root_policy
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.db_path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> 'AssetRegistry':
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _assert_canonical(self, rel_path: str) -> None:
        root = Path(rel_path).parts[0]
        if root in self.root_policy.derived:
            raise DerivedRootError(f'derived root file must not enter the registry: {rel_path}')
        if root not in self.root_policy.accepted:
            raise AssetRegistryError(f'file outside admitted library roots: {rel_path}')

    def reconcile(self, files: dict[str, FileRecord], *, attachments: list[ResourceAttachment] | None=None, merges: list[GroupMerge] | None=None, unresolved: list[UnresolvedRecord] | None=None, batch_items: list[BatchItem] | None=None, mode: str='bootstrap') -> ReconcileReport:
        """Reconcile current library files into the registry.

        ``files`` maps rel-path (relative to the library root) -> FileRecord.
        Safe to call repeatedly (rerun never re-mints known assets).
        """
        attachments = attachments or []
        merges = merges or []
        unresolved = unresolved or []
        batch_items = batch_items or []
        report = ReconcileReport(run_id=f"run_{time.strftime('%Y%m%d_%H%M%S')}")
        started = _now()
        self.db.execute('INSERT OR REPLACE INTO registry_run(run_id, started_at, mode) VALUES (?,?,?)', (report.run_id, started, mode))
        for rel in files:
            self._assert_canonical(rel)
        now = _now()
        attached_paths = {a.rel_path for a in attachments}
        unresolved_paths = {u.path for u in unresolved if u.path in files}
        excluded = attached_paths | unresolved_paths
        prev_by_path: dict[str, sqlite3.Row] = {r['current_path']: r for r in self.db.execute('SELECT * FROM resource_occurrence')}
        occ_asset: dict[str, str] = {}
        for row in self.db.execute('SELECT ar.occurrence_id, ar.asset_id FROM asset_resource ar JOIN photo_asset pa ON pa.asset_id = ar.asset_id WHERE pa.merged_into IS NULL'):
            occ_asset[row['occurrence_id']] = row['asset_id']
        seen_occ_ids: set[str] = set()
        path_to_occ_id: dict[str, str] = {}
        new_paths: list[str] = []
        for rel in sorted(files):
            rec = files[rel]
            prev = prev_by_path.get(rel)
            if prev is None:
                new_paths.append(rel)
                continue
            occ_id = prev['occurrence_id']
            if prev['content_sha256'] == rec.sha256:
                report.occurrences_unchanged += 1
                self.db.execute("UPDATE resource_occurrence SET state='current', last_seen_at=? WHERE occurrence_id=?", (now, occ_id))
            else:
                report.occurrences_revised += 1
                self._upsert_media_content(rec.sha256, rec.bytes, rel, now)
                self.db.execute("UPDATE resource_occurrence SET content_sha256=?, stat_signature=?, state='current', last_seen_at=? WHERE occurrence_id=?", (rec.sha256, rec.stat_signature, now, occ_id))
            seen_occ_ids.add(occ_id)
            path_to_occ_id[rel] = occ_id
        missing_by_sha: dict[str, list[str]] = {}
        for path, row in prev_by_path.items():
            if path not in files:
                missing_by_sha.setdefault(row['content_sha256'], []).append(path)
        current_paths_by_sha: dict[str, list[str]] = {}
        for rel, rec in files.items():
            current_paths_by_sha.setdefault(rec.sha256, []).append(rel)
        claimed_moves: set[str] = set()
        for rel in new_paths:
            rec = files[rel]
            move_candidates = [p for p in missing_by_sha.get(rec.sha256, []) if p not in claimed_moves]
            live_dups = current_paths_by_sha.get(rec.sha256, [])
            if len(move_candidates) == 1 and len(live_dups) == 1:
                src_path = move_candidates[0]
                src_row = prev_by_path[src_path]
                occ_id = src_row['occurrence_id']
                report.occurrences_moved += 1
                claimed_moves.add(src_path)
                self.db.execute("UPDATE resource_occurrence SET current_path=?, import_root=?, stat_signature=?, state='current', last_seen_at=?, moved_from=? WHERE occurrence_id=?", (rel, Path(rel).parts[0], rec.stat_signature, now, src_path, occ_id))
                seen_occ_ids.add(occ_id)
                path_to_occ_id[rel] = occ_id
                continue
            if len(move_candidates) > 1:
                report.unresolved += 1
                report.notes.append(f'ambiguous move for {rel}: {len(move_candidates)} missing same-content candidates')
                self._record_unresolved(UnresolvedRecord(item=f'ambiguous_move:{rel}', kind='ambiguous_move', path=rel, reason='multiple missing occurrences share the content', candidates=sorted(move_candidates)))
            report.occurrences_new += 1
            occ_id = _mint_occurrence_id(rel)
            while self.db.execute('SELECT 1 FROM resource_occurrence WHERE occurrence_id=?', (occ_id,)).fetchone():
                occ_id = _mint_occurrence_id(occ_id)
            self._upsert_media_content(rec.sha256, rec.bytes, rel, now)
            self.db.execute("INSERT INTO resource_occurrence (occurrence_id, current_path, import_root, stat_signature,  content_sha256, state, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,'current',?,?)", (occ_id, rel, Path(rel).parts[0], rec.stat_signature, rec.sha256, now, now))
            seen_occ_ids.add(occ_id)
            path_to_occ_id[rel] = occ_id
        for row in self.db.execute('SELECT occurrence_id FROM resource_occurrence'):
            if row['occurrence_id'] not in seen_occ_ids:
                self.db.execute("UPDATE resource_occurrence SET state='missing', last_seen_at=? WHERE occurrence_id=?", (now, row['occurrence_id']))
                report.occurrences_missing += 1
        group_files: dict[tuple[str, str], list[str]] = {}
        for rel in sorted(files):
            if rel in excluded:
                continue
            if classify_variant(rel, True) is None:
                if media_type_of(rel) == 'other':
                    report.non_photo_files += 1
                    self._record_unresolved(UnresolvedRecord(item=f'non_photo_file:{rel}', kind='non_photo_file', path=rel, reason='extension is not image/video/sidecar'))
                continue
            group_files.setdefault(group_key(rel), []).append(rel)
        parent: dict[tuple[str, str], tuple[str, str]] = {g: g for g in group_files}

        def find(g):
            while parent[g] != g:
                parent[g] = parent[parent[g]]
                g = parent[g]
            return g

        def union(a, b):
            ra, rb = (find(a), find(b))
            if ra != rb:
                parent[rb] = ra
        merge_pairs: list[tuple[tuple[str, str], tuple[str, str]]] = []
        for m in merges:
            ga, gb = (group_key(m.path_a), group_key(m.path_b))
            if ga in parent and gb in parent and (ga != gb):
                union(ga, gb)
                merge_pairs.append((ga, gb))
        comps: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for g in group_files:
            comps.setdefault(find(g), []).append(g)
        for root_g in sorted(comps):
            members = sorted(comps[root_g])
            all_files = [f for g in members for f in group_files[g]]
            existing_assets: set[str] = set()
            for rel in all_files:
                occ_id = path_to_occ_id.get(rel)
                if occ_id and occ_id in occ_asset:
                    existing_assets.add(occ_asset[occ_id])
            existing_assets = {a for a in existing_assets if self._asset_alive(a)}
            origin_key = '|'.join((f'{g[0]}|{g[1]}' for g in members))
            if len(existing_assets) == 1:
                asset_id = existing_assets.pop()
                report.assets_unchanged += 1
            elif len(existing_assets) > 1:
                asset_id = self._merge_assets(existing_assets, now)
                report.assets_merged += 1
                report.notes.append(f'merged {len(existing_assets)} assets into {asset_id} (origin {origin_key})')
            else:
                asset_id = self._mint_asset(origin_key, now)
                report.assets_minted += 1
            group_has_still = any((classify_variant(rel, True) == VARIANT_ORIGINAL for rel in all_files))
            for rel in sorted(all_files):
                variant = classify_variant(rel, group_has_still)
                if variant is None:
                    continue
                self._link_resource(asset_id, path_to_occ_id[rel], variant, BASIS_BASE_RULE, now)
            self._finalize_asset(asset_id, now)
        for att in attachments:
            occ_id = path_to_occ_id.get(att.rel_path)
            tgt_occ = path_to_occ_id.get(att.target_still_path)
            if not occ_id or not tgt_occ:
                report.notes.append(f'attachment skipped (path unknown): {att.rel_path}')
                continue
            tgt_asset_row = self.db.execute('SELECT asset_id FROM asset_resource WHERE occurrence_id=? AND asset_id IN (SELECT asset_id FROM photo_asset WHERE merged_into IS NULL)', (tgt_occ,)).fetchone()
            if not tgt_asset_row:
                report.notes.append(f'attachment target has no live asset: {att.target_still_path}')
                continue
            variant = classify_variant(att.rel_path, True) or VARIANT_SIDECAR
            self._link_resource(tgt_asset_row['asset_id'], occ_id, variant, att.basis, now)
            report.attachments_applied += 1
            self._finalize_asset(tgt_asset_row['asset_id'], now)
        for item in batch_items:
            asset_ids: set[str] = set()
            for rel in item.rel_paths:
                occ_id = path_to_occ_id.get(rel)
                if not occ_id:
                    prev = prev_by_path.get(rel)
                    occ_id = prev['occurrence_id'] if prev else None
                if not occ_id:
                    continue
                row = self.db.execute('SELECT ar.asset_id FROM asset_resource ar JOIN photo_asset pa ON pa.asset_id = ar.asset_id WHERE ar.occurrence_id=? AND pa.merged_into IS NULL', (occ_id,)).fetchone()
                if row:
                    asset_ids.add(row['asset_id'])
            if len(asset_ids) == 1:
                aid = asset_ids.pop()
                status = 'one_to_one'
                self.db.execute('INSERT OR REPLACE INTO batch_asset_link (batch_id, legacy_key, asset_id, mapping_status,  capture_date, capture_source) VALUES (?,?,?,?,?,?)', (item.batch_id, item.legacy_key, aid, status, item.capture_date, item.capture_source))
                report.batch_links += 1
                if item.capture_date:
                    self.db.execute('UPDATE photo_asset SET capture_date=?, capture_source=?, capture_precision=?, updated_at=? WHERE asset_id=? AND (capture_date IS NULL OR capture_date=?)', (item.capture_date, item.capture_source, 'organizer_batch', now, aid, item.capture_date))
            elif not asset_ids:
                self.db.execute("INSERT OR REPLACE INTO batch_asset_link (batch_id, legacy_key, asset_id, mapping_status) VALUES (?,?,NULL,'no_existing_files')", (item.batch_id, item.legacy_key))
            else:
                self.db.execute("INSERT OR REPLACE INTO batch_asset_link (batch_id, legacy_key, asset_id, mapping_status) VALUES (?,?,NULL,'ambiguous')", (item.batch_id, item.legacy_key))
        for u in unresolved:
            self._record_unresolved(u)
            report.unresolved += 1
        self.db.execute('UPDATE registry_run SET finished_at=?, stats_json=? WHERE run_id=?', (_now(), json.dumps({'files_seen': report.files_seen, 'occurrences_unchanged': report.occurrences_unchanged, 'occurrences_revised': report.occurrences_revised, 'occurrences_moved': report.occurrences_moved, 'occurrences_new': report.occurrences_new, 'occurrences_missing': report.occurrences_missing, 'assets_minted': report.assets_minted, 'assets_unchanged': report.assets_unchanged, 'assets_merged': report.assets_merged, 'attachments_applied': report.attachments_applied, 'batch_links': report.batch_links, 'unresolved': report.unresolved, 'non_photo_files': report.non_photo_files}, ensure_ascii=False), report.run_id))
        self.db.commit()
        return report

    def register_file(self, source: str | Path, dest_root: str | Path, *, library_root: str | Path | None=None) -> ImportResult:
        """Incrementally register ONE external photo file (safe single import).

        Copies ``source`` (must be outside the photo library, not a symlink,
        not generated/derived, real JPEG/PNG bytes) into the managed import
        root ``dest_root`` and mints a PhotoAsset + occurrence through the
        same identity rules as reconcile. Guarantees:

        * never calls full reconcile; no other occurrence's state / path /
          canonical is modified (INSERT-only plus finalization of the new
          asset itself)
        * content dedup: if the sha is already registered under exactly one
          live asset, that asset_id is returned and nothing is copied;
          ambiguous multi-asset content fails closed
        * same name + different content: never overwrites — a safe suffixed
          destination name is claimed exclusively (os.link, atomic)
        * the source file is only ever read, never modified or moved
        * DB failure rolls back completely and removes the copy it made
          (pre-existing files are never deleted)
        """
        source = Path(source)
        dest_root = Path(dest_root)
        if library_root is not None:
            library_root = Path(library_root).resolve()
        if source.is_symlink():
            raise AssetRegistryError(f'source must not be a symlink: {source}')
        if not source.exists():
            raise AssetRegistryError(f'source not found: {source}')
        if not source.is_file():
            raise AssetRegistryError(f'source is not a regular file: {source}')
        src_real = source.resolve()
        if library_root is not None and src_real.is_relative_to(library_root):
            raise AssetRegistryError(f'source is inside the photo library — use the inventory/reconcile flow (single import is for external photos only): {src_real}')
        if _looks_generated(src_real):
            raise GeneratedSourceError(f'generated/derived source rejected (photomosaic-style metadata present): {src_real}')
        ext = ext_of(src_real.name)
        if ext not in IMPORT_SOURCE_EXT:
            raise AssetRegistryError(f'unsupported extension for single import: {src_real.name} (allowed: {sorted(IMPORT_SOURCE_EXT)})')
        stem = src_real.stem
        if is_edit(stem) or RE_ORIG.search(stem):
            raise AssetRegistryError(f'edited/original-marker naming implies a base photo (use the batch flow for its group): {src_real.name}')
        sniff = sniff_magic(src_real)
        if sniff.format not in ('jpeg', 'png') or sniff.mismatch:
            raise AssetRegistryError(f'source is not a well-formed JPEG/PNG file: {src_real} (sniffed: {sniff.format})')
        if dest_root.name not in self.root_policy.managed:
            raise AssetRegistryError(f'dest_root must be a managed import root (one of {sorted(self.root_policy.managed)}): {dest_root}')
        if dest_root.is_symlink():
            raise AssetRegistryError(f'dest_root must not be a symlink: {dest_root}')
        if library_root is not None and dest_root.parent.resolve() != library_root:
            raise AssetRegistryError(f'dest_root must be a direct child of the library root: {dest_root} (library root: {library_root})')
        dest_dir = dest_root
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = dest_dir.resolve()
        sha = _sha256_file(src_real)
        size = src_real.stat().st_size
        match = self._live_assets_for_content(sha)
        if len(match) > 1:
            raise AssetRegistryError(f'content already registered under multiple live assets ({len(match)}): {sorted(match)} — operator must disambiguate')
        if len(match) == 1:
            return self._reuse_result(match[0], sha, size, src_real, 'reused_content')
        try:
            placed, created_by_us = self._place_copy(src_real, sha, dest_dir)
        except OSError as e:
            raise AssetRegistryError(f'copy into managed root failed: {e}') from e
        rel = f'{dest_dir.name}/{placed.name}'
        last_error: Exception | None = None
        try:
            for _attempt in range(3):
                try:
                    return self._register_new(src_real, placed, created_by_us, rel, sha, size)
                except sqlite3.OperationalError as e:
                    self.db.rollback()
                    last_error = e
            raise AssetRegistryError(f'registry stayed busy, single import aborted: {last_error}')
        except Exception:
            if created_by_us:
                _safe_unlink(placed)
            raise

    @staticmethod
    def _dest_candidates(name: str) -> list[str]:
        p = Path(name)
        return [name] + [f'{p.stem} {i}{p.suffix}' for i in range(1, 1000)]

    def _place_copy(self, src_real: Path, sha: str, dest_dir: Path) -> tuple[Path, bool]:
        """Copy source into dest_dir claiming an exclusive destination name.

        The copy lands in a private temp file first, is verified byte-for-byte,
        then atomically linked into place (os.link never overwrites). A name
        already holding different content never gets touched — the next safe
        suffixed candidate is used instead.
        """
        tmp = dest_dir / f'.import-{uuid.uuid4().hex}.tmp'
        try:
            with open(tmp, 'xb') as out, open(src_real, 'rb') as inp:
                for chunk in iter(lambda: inp.read(1 << 20), b''):
                    out.write(chunk)
            if _sha256_file(tmp) != sha:
                raise AssetRegistryError('copy verification failed (sha mismatch after copy)')
            placed: Path | None = None
            created_by_us = False
            for cand_name in self._dest_candidates(src_real.name):
                cand = dest_dir / cand_name
                if cand.is_symlink():
                    continue
                try:
                    os.link(tmp, cand)
                except OSError as e:
                    if not _is_eexists(e):
                        raise AssetRegistryError(f'cannot claim destination {cand}: {e}') from e
                    try:
                        if not cand.is_symlink() and _sha256_file(cand) == sha:
                            placed = cand
                            break
                    except OSError:
                        pass
                    continue
                placed = cand
                created_by_us = True
                break
            if placed is None:
                raise AssetRegistryError(f'no free destination name in managed root for: {src_real.name}')
            return (placed, created_by_us)
        finally:
            _safe_unlink(tmp)

    def _live_assets_for_content(self, sha: str) -> list[str]:
        return [r['asset_id'] for r in self.db.execute("SELECT DISTINCT pa.asset_id FROM photo_asset pa JOIN asset_resource ar ON ar.asset_id = pa.asset_id JOIN resource_occurrence ro ON ro.occurrence_id = ar.occurrence_id WHERE pa.merged_into IS NULL AND ro.state='current' AND ro.content_sha256=?", (sha,))]

    def _reuse_result(self, asset_id: str, sha: str, size: int, src_real: Path, action: str) -> ImportResult:
        row = self.db.execute('SELECT pa.canonical_occurrence_id, ro.current_path FROM photo_asset pa LEFT JOIN resource_occurrence ro   ON ro.occurrence_id = pa.canonical_occurrence_id WHERE pa.asset_id=?', (asset_id,)).fetchone()
        run_id = self._record_import_run('single_import_reuse', {'action': action, 'source': str(src_real), 'content_sha256': sha, 'asset_id': asset_id})
        return ImportResult(asset_id=asset_id, action=action, rel_path=row['current_path'] or '', content_sha256=sha, bytes=size, occurrence_id=None, canonical_occurrence_id=row['canonical_occurrence_id'], copied=False)

    def _record_import_run(self, mode: str, stats: dict) -> str:
        run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        self.db.execute('INSERT INTO registry_run(run_id, started_at, mode, stats_json) VALUES (?,?,?,?)', (run_id, _now(), mode, json.dumps(stats, ensure_ascii=False)))
        self.db.execute('UPDATE registry_run SET finished_at=? WHERE run_id=?', (_now(), run_id))
        self.db.commit()
        return run_id

    def _register_new(self, src_real: Path, placed: Path, created_by_us: bool, rel: str, sha: str, size: int) -> ImportResult:
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            match = self._live_assets_for_content(sha)
            if len(match) > 1:
                raise AssetRegistryError(f'content became registered under multiple live assets: {sorted(match)}')
            if len(match) == 1:
                self.db.rollback()
                if created_by_us:
                    _safe_unlink(placed)
                return self._reuse_result(match[0], sha, size, src_real, 'reused_content')
            occ = self.db.execute('SELECT occurrence_id, content_sha256 FROM resource_occurrence WHERE current_path=?', (rel,)).fetchone()
            if occ is not None:
                if occ['content_sha256'] != sha:
                    raise AssetRegistryError(f'destination path already registered with different content: {rel}')
                self.db.rollback()
                asset_row = self.db.execute('SELECT pa.asset_id FROM asset_resource ar JOIN photo_asset pa ON pa.asset_id = ar.asset_id WHERE ar.occurrence_id=? AND pa.merged_into IS NULL', (occ['occurrence_id'],)).fetchone()
                if asset_row is None:
                    raise AssetRegistryError(f"destination occurrence has no live asset: {rel} ({occ['occurrence_id']})")
                return self._reuse_result(asset_row['asset_id'], sha, size, src_real, 'reused_path')
            now = _now()
            occ_id = _mint_occurrence_id(rel)
            while self.db.execute('SELECT 1 FROM resource_occurrence WHERE occurrence_id=?', (occ_id,)).fetchone():
                occ_id = _mint_occurrence_id(occ_id)
            self._upsert_media_content(sha, size, rel, now)
            self.db.execute("INSERT INTO resource_occurrence (occurrence_id, current_path, import_root, stat_signature,  content_sha256, state, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,'current',?,?)", (occ_id, rel, Path(rel).parts[0], str(placed.stat().st_mtime_ns), sha, now, now))
            origin_key = f'{Path(rel).parts[0]}|{base_of(Path(rel).stem)}'
            asset_id = self._mint_asset(origin_key, now)
            variant = classify_variant(rel, True)
            if variant is None:
                raise AssetRegistryError(f'internal: unclassifiable single-import variant: {rel}')
            self._link_resource(asset_id, occ_id, variant, BASIS_SINGLE_IMPORT, now)
            self._finalize_asset(asset_id, now)
            canon = self.db.execute('SELECT canonical_occurrence_id, media_type FROM photo_asset WHERE asset_id=?', (asset_id,)).fetchone()
            run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            self.db.execute('INSERT INTO registry_run(run_id, started_at, mode, stats_json) VALUES (?,?,?,?)', (run_id, now, 'single_import', json.dumps({'action': 'minted', 'source': str(src_real), 'content_sha256': sha, 'asset_id': asset_id, 'occurrence_id': occ_id, 'rel_path': rel, 'variant': variant, 'relation_basis': BASIS_SINGLE_IMPORT, 'bytes': size}, ensure_ascii=False)))
            self.db.execute('UPDATE registry_run SET finished_at=? WHERE run_id=?', (_now(), run_id))
            self.db.commit()
            return ImportResult(asset_id=asset_id, action='minted', rel_path=rel, content_sha256=sha, bytes=size, occurrence_id=occ_id, canonical_occurrence_id=canon['canonical_occurrence_id'], copied=True)
        except Exception:
            self.db.rollback()
            raise

    def _upsert_media_content(self, sha: str, size: int, rel: str, now: str) -> None:
        row = self.db.execute('SELECT 1 FROM media_content WHERE content_sha256=?', (sha,)).fetchone()
        if row:
            self.db.execute('UPDATE media_content SET last_seen_at=? WHERE content_sha256=?', (now, sha))
        else:
            self.db.execute('INSERT INTO media_content (content_sha256, bytes, media_type, first_seen_at, last_seen_at) VALUES (?,?,?,?,?)', (sha, size, media_type_of(rel), now, now))

    def _asset_alive(self, asset_id: str) -> bool:
        row = self.db.execute('SELECT merged_into FROM photo_asset WHERE asset_id=?', (asset_id,)).fetchone()
        return bool(row) and row['merged_into'] is None

    def _mint_asset(self, origin_key: str, now: str) -> str:
        existing = self.db.execute('SELECT asset_id FROM photo_asset WHERE origin_key=?', (origin_key,)).fetchone()
        if existing:
            asset_id = existing['asset_id']
            self.db.execute('UPDATE photo_asset SET merged_into=NULL, updated_at=? WHERE asset_id=?', (now, asset_id))
            return asset_id
        asset_id = _mint_asset_id(origin_key)
        while self.db.execute('SELECT 1 FROM photo_asset WHERE asset_id=?', (asset_id,)).fetchone():
            asset_id = _mint_asset_id(asset_id)
        self.db.execute("INSERT INTO photo_asset (asset_id, origin_key, media_type, created_at, updated_at) VALUES (?,?, 'photo', ?, ?)", (asset_id, origin_key, now, now))
        return asset_id

    def _merge_assets(self, asset_ids: set[str], now: str) -> str:
        rows = self.db.execute(f"SELECT asset_id, created_at FROM photo_asset WHERE asset_id IN ({','.join('?' * len(asset_ids))}) ORDER BY created_at, asset_id", tuple(sorted(asset_ids))).fetchall()
        survivor = rows[0]['asset_id']
        for row in rows[1:]:
            loser = row['asset_id']
            self.db.execute('UPDATE asset_resource SET asset_id=? WHERE asset_id=?', (survivor, loser))
            self.db.execute('UPDATE photo_asset SET merged_into=?, updated_at=? WHERE asset_id=?', (survivor, now, loser))
        return survivor

    def _link_resource(self, asset_id: str, occ_id: str, variant: str, basis: str, now: str) -> None:
        self.db.execute('INSERT OR IGNORE INTO asset_resource (asset_id, occurrence_id, variant, relation_basis, attached_at) VALUES (?,?,?,?,?)', (asset_id, occ_id, variant, basis, now))

    def _finalize_asset(self, asset_id: str, now: str) -> None:
        """Recompute media_type + canonical original still for an asset."""
        rows = self.db.execute("SELECT ro.occurrence_id, ro.current_path, ar.variant FROM asset_resource ar JOIN resource_occurrence ro ON ro.occurrence_id = ar.occurrence_id WHERE ar.asset_id=? AND ro.state='current' ORDER BY ro.current_path", (asset_id,)).fetchall()
        variants = {r['variant'] for r in rows}
        has_still = VARIANT_ORIGINAL in variants
        if not has_still and VARIANT_EDITED in variants:
            media_type = 'photo_no_original_still'
        elif not has_still and VARIANT_VIDEO in variants:
            media_type = 'video'
        elif not has_still:
            media_type = 'unusable'
        elif VARIANT_LIVE in variants or VARIANT_VIDEO_EDITED in variants:
            media_type = 'live_photo'
        else:
            media_type = 'photo'
        canonical = None
        stills = [r for r in rows if r['variant'] == VARIANT_ORIGINAL]
        if stills:
            stills.sort(key=lambda r: (STILL_EXT_PRIORITY.index(ext_of(r['current_path'])) if ext_of(r['current_path']) in STILL_EXT_PRIORITY else len(STILL_EXT_PRIORITY), r['current_path']))
            canonical = stills[0]['occurrence_id']
        self.db.execute('UPDATE photo_asset SET media_type=?, canonical_occurrence_id=?, updated_at=? WHERE asset_id=?', (media_type, canonical, now, asset_id))

    def _record_unresolved(self, u: UnresolvedRecord) -> None:
        self.db.execute('INSERT OR REPLACE INTO unresolved_attachment (item, kind, path, reason, candidates_json) VALUES (?,?,?,?,?)', (u.item, u.kind, u.path, u.reason, json.dumps(u.candidates, ensure_ascii=False)))

    def build_manifest(self, out_path: str | Path, library_root: str | Path) -> dict:
        """Write rehearsal ``asset_manifest/1.0`` (one row per PhotoAsset that
        has a usable original still; path = canonical original still).

        Deterministic sort by asset_id; atomic write (tmp + os.replace).
        """
        library_root = Path(library_root)
        rows = self.db.execute("SELECT pa.asset_id, ro.current_path, ro.content_sha256, mc.bytes FROM photo_asset pa JOIN resource_occurrence ro   ON ro.occurrence_id = pa.canonical_occurrence_id  AND ro.state='current' JOIN media_content mc ON mc.content_sha256 = ro.content_sha256 WHERE pa.merged_into IS NULL ORDER BY pa.asset_id").fetchall()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + '.tmp')
        stats = {'rows': 0, 'skipped_no_still': 0, 'by_media_type': {}, 'out_path': str(out_path)}
        seen_ids: set[str] = set()
        with open(tmp, 'w', encoding='utf-8') as f:
            for row in rows:
                asset_id = row['asset_id']
                if asset_id in seen_ids:
                    raise AssetRegistryError(f'duplicate asset_id in manifest: {asset_id}')
                seen_ids.add(asset_id)
                abs_path = str(library_root / row['current_path'])
                if not os.path.exists(abs_path):
                    raise AssetRegistryError(f'manifest path not accessible: {abs_path}')
                entry = {'schema': MANIFEST_SCHEMA, 'asset_id': asset_id, 'path': abs_path, 'content_sha256': row['content_sha256'], 'file_size': row['bytes'], 'provenance': 'asset asset registry bootstrap v1'}
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
                stats['rows'] += 1
            for row in self.db.execute('SELECT media_type, COUNT(*) AS n FROM photo_asset WHERE merged_into IS NULL GROUP BY media_type'):
                stats['by_media_type'][row['media_type']] = row['n']
                if row['media_type'] in ('photo_no_original_still', 'video', 'unusable'):
                    stats['skipped_no_still'] += row['n']
        os.replace(tmp, out_path)
        return stats

    def summary_counts(self) -> dict:
        out: dict = {}
        for table in ('media_content', 'resource_occurrence', 'photo_asset', 'asset_resource', 'batch_asset_link', 'unresolved_attachment', 'registry_run'):
            out[table] = self.db.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n']
        out['occurrences_current'] = self.db.execute("SELECT COUNT(*) AS n FROM resource_occurrence WHERE state='current'").fetchone()['n']
        out['assets_alive'] = self.db.execute('SELECT COUNT(*) AS n FROM photo_asset WHERE merged_into IS NULL').fetchone()['n']
        out['assets_merged'] = self.db.execute('SELECT COUNT(*) AS n FROM photo_asset WHERE merged_into IS NOT NULL').fetchone()['n']
        out['duplicate_asset_ids'] = self.db.execute('SELECT COUNT(*) AS n FROM (SELECT asset_id FROM photo_asset GROUP BY asset_id HAVING COUNT(*) > 1)').fetchone()['n']
        return out

    def variant_counts(self) -> dict:
        return {r['variant']: r['n'] for r in self.db.execute('SELECT variant, COUNT(*) AS n FROM asset_resource JOIN photo_asset pa ON pa.asset_id = asset_resource.asset_id WHERE pa.merged_into IS NULL GROUP BY variant ORDER BY variant')}

    def exact_duplicate_contents(self) -> list[dict]:
        """Content shas shared by >1 live asset (legal: distinct logical assets)."""
        return [dict(r) for r in self.db.execute('SELECT ro.content_sha256 AS content, COUNT(DISTINCT pa.asset_id) AS assets FROM asset_resource ar JOIN resource_occurrence ro ON ro.occurrence_id = ar.occurrence_id JOIN photo_asset pa ON pa.asset_id = ar.asset_id WHERE pa.merged_into IS NULL GROUP BY ro.content_sha256 HAVING assets > 1 ORDER BY assets DESC, content')]

    def assets_without_still(self) -> list[dict]:
        return [dict(r) for r in self.db.execute('SELECT pa.asset_id, pa.media_type, pa.origin_key FROM photo_asset pa WHERE pa.merged_into IS NULL AND pa.canonical_occurrence_id IS NULL ORDER BY pa.asset_id')]

    def unresolved_rows(self) -> list[dict]:
        return [dict(r) for r in self.db.execute('SELECT item, kind, path, reason, candidates_json FROM unresolved_attachment ORDER BY item')]

    def asset_of_path(self, rel_path: str) -> str | None:
        row = self.db.execute("SELECT ar.asset_id FROM asset_resource ar JOIN resource_occurrence ro ON ro.occurrence_id = ar.occurrence_id JOIN photo_asset pa ON pa.asset_id = ar.asset_id WHERE ro.current_path=? AND ro.state='current' AND pa.merged_into IS NULL", (rel_path,)).fetchone()
        return row['asset_id'] if row else None

@dataclass(frozen=True)
class RootPolicy:
    accepted: frozenset[str]
    managed: frozenset[str]
    derived: frozenset[str] = frozenset()

    def __post_init__(self):
        for key in ('accepted', 'managed', 'derived'):
            object.__setattr__(self, key, frozenset(getattr(self, key)))
        if not self.accepted or not self.managed <= self.accepted or self.accepted & self.derived:
            raise ValueError('Invalid library root policy')
        for root in self.accepted | self.derived:
            if not root or root in ('.', '..') or '/' in root or ('\\' in root):
                raise ValueError('Library roots must be single relative directory names')
__all__ = ['MANIFEST_SCHEMA', 'IMG_EXT', 'VID_EXT', 'SIDECAR_EXT', 'STILL_EXT_PRIORITY', 'RE_EDIT', 'RE_ORIG', 'VARIANT_ORIGINAL', 'VARIANT_EDITED', 'VARIANT_LIVE', 'VARIANT_VIDEO_EDITED', 'VARIANT_SIDECAR', 'VARIANT_VIDEO', 'BASIS_BASE_RULE', 'BASIS_SINGLE_IMPORT', 'IMPORT_SOURCE_EXT', '_REGISTRY_NAMESPACE', 'AssetRegistryError', 'DerivedRootError', 'GeneratedSourceError', 'base_of', 'is_edit', 'ext_of', 'media_type_of', 'classify_variant', 'group_key', '_mint_asset_id', '_mint_occurrence_id', '_now', '_sha256_file', '_looks_generated', '_safe_unlink', '_is_eexists', 'FileRecord', 'ResourceAttachment', 'GroupMerge', 'UnresolvedRecord', 'BatchItem', 'ReconcileReport', 'ImportResult', '_SCHEMA', 'AssetRegistry', 'RootPolicy']
