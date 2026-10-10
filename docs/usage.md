# Public V0 fresh-workspace usage

Library entry point: `person_engine.public_v0`; local JSON CLI: `python -m person_engine.public_v0`. A [FastAPI service](local-api.md) and [Docker runtime](docker-runtime.md) expose the same core operations. Use CPython 3.12, portable JPEG/PNG decoding, one workspace writer and bounded matching of up to 512 faces. Linux HEIC is unsupported.

Initialize a new empty directory; adopting or migrating an existing workspace is unsupported. V0 creates `workspace.json`, `state/public_v0.sqlite`, `content/` and `exports/`. The engine owns source bindings, logical asset IDs, facts and publication receipts. All domain rows share this one fresh SQLite authority so review apply is atomic. No old DB schema, admission receipt, backup, private roster or consumer state is imported.

```python
from pathlib import Path
import hashlib
from person_engine.public_v0 import (
    initialize, CONTRACT, register, LocalModels, ingest, match,
    create_session, fetch_session, save_decisions, apply,
    publish_snapshot, read_snapshot, export_snapshot, person_command,
)

ws = initialize(Path("fresh-person-workspace"))
photo = Path("my-photo.png")
raw = photo.read_bytes()
registration = register(ws, {
    "contract": CONTRACT,
    "workspace_id": ws.workspace_id,
    "request_id": "register-001",
    "source_namespace": "my-library",
    "source_ref": "photo-001",
    "content_sha256": hashlib.sha256(raw).hexdigest(),
    "bytes": len(raw),
    "media_type": "image/png",
    "expected_revision": ws.basis()["revision"],
}, photo)
```

All identifiers except user display names use the bounded opaque identifier alphabet `A-Za-z0-9_.:-`; the first character must be alphanumeric. Use source IDs, not filesystem paths, as `source_ref`. For JPEG use `image/jpeg`. The path is a separate local argument and is not embedded in receipts or snapshots. Same namespace/ref cannot be rebound silently. Same bytes under a different ref produce a different asset while sharing content bytes. Exact request replay returns the saved receipt even if the source file is no longer present. New-key requests require the current revision. Existing published content is never overwritten.

Real inference requires explicit local model paths/hashes and policy. The following uses a caller-maintained configuration object, without introducing numeric defaults:

```python
provider = LocalModels(**model_configuration)
# Required keys: detector_path, embedding_path, detector_sha256,
# embedding_sha256, confidence, nms, top_k.
ingested = ingest(ws, registration["asset_id"], provider,
                  "ingest-001", ws.basis())
result = match(ws, "match-001", ws.basis(), candidate_policy)
# candidate_policy requires match_similarity, match_margin, cluster_similarity.
review = create_session(ws, result["run_id"], "review-001")
candidates = fetch_session(ws, review["session_id"])["source"]["candidates"]
```

The first roster is empty. Results contain provisional unknown groups and no stable person IDs. A user may explicitly create the first person through NEW_PERSON, or use `person_command(CREATE)` and then confirm EXISTING_PERSON. An example **human-selected** decision on one returned candidate:

```python
face = candidates[0]  # Handle NO_FACES / empty candidates before selecting.
saved = save_decisions(ws, review["session_id"], review["revision"], [{
    "face_id": face["face_id"],
    "action": "NEW_PERSON",
    "expected_state": {"state": face["state"], "person_id": face["person_id"]},
    "display_name": "Person A",
    "person_key": "person-a",
    "enroll_reference": True,  # Explicit human choice, never automatic.
}], "save-001")
applied = apply(ws, review["session_id"], saved["revision"], "apply-001")
publication = publish_snapshot(ws, "publish-001", ws.basis())
exported = export_snapshot(ws, publication["snapshot_id"])
# A future consumer reads ws.home / exported['relative_path'] as ordinary JSON.
assert read_snapshot(ws, publication["snapshot_id"])["snapshot_id"] == publication["snapshot_id"]
```

A display name and `person_key` may be shared by several NEW_PERSON decisions in one session to create one human-selected person. Existing persons are immutable IDs. `person_command` supports CREATE, RENAME and RETIRE, with request-key replay and expected source basis; retirement refuses active assignments/references and preserves audit history. `ws.inspect()` reads assets, people, faces/states and the current source basis.

Decision contracts (every decision includes `face_id` and exact `expected_state`):

| Action | Additional fields | Result |
|---|---|---|
| EXISTING_PERSON | person_id, optional enroll_reference | Confirm an active person; cannot silently reverse a rejection or terminal fact |
| NEW_PERSON | display_name, person_key, optional enroll_reference | Human creates a stable person; inference never does |
| REJECT | person_id | Negative per-person fact; unassigns that person if currently assigned; preserves unrelated human state |
| UNASSIGN | none | Preserves face, clears assignment/reference, records UNASSIGNED |
| OUTSIDER | none | Real face outside current roster; not a false detection |
| IGNORE_FACE | none | Human false-positive decision; clears assignment/reference and records IGNORED |
| CORRECTION | to = {state, person_id}, optional enroll_reference | Explicit old→new human state; assigned target must already exist; reverses matching target rejection if needed |

Same save key/payload returns its original revision receipt; changed payload conflicts. A new save key with an old revision fails. Any intervening domain change invalidates the frozen review basis. Duplicate apply returns exactly the original receipt (semantic noop); no new human facts/references/events are created. An exception or process death rolls back the whole transaction; fresh SQLite journal recovery uses the same kernel writer lease. No external backup/replay is required. New-key apply of an already applied revision also returns the original domain receipt.

Publication and file export have separate atomic boundaries. A snapshot's full canonical JSON and receipt commit in SQLite together and cannot be updated/deleted through the schema. `publish_snapshot` does not imply that an export file already exists. `export_snapshot` creates one complete JSON file with an atomic no-overwrite link; a failed export can be retried from the authoritative published bytes. Consumers do not need SQLite, Python, Organizer or a receipt acknowledgement to read that JSON. A matching filename with different bytes is a conflict, never an overwrite. Interrupted staging files are not published snapshots.

The snapshot is complete, not a delta: stable workspace/person/face IDs, version, schema/producer versions, human assignments, explicit states, anonymous-by-construction provenance fields, source digest and timestamp. It contains no photo path, model path, crop, embedding or consumer policy. User-provided display names and source IDs remain user content; the engine does not anonymize those on export.

Model-space changes, workspace migration, HEIC on Linux, large-batch scheduling and multi-user authorization are outside this V0. See [calibration boundary](calibration.md).

Reproduce the anonymous core proof from this repository:

```sh
.venv/bin/python -B scripts/run_release_tests.py --report /tmp/public-v0-tests.json
.venv/bin/python -B scripts/build_dependency_graph.py --output /tmp/public-v0-graph.json
```

The test runner uses fresh temporary workspaces, synthetic images/vectors, a public import allowlist, and file/SQLite/network audit guards. It does not discover historical tests or read actual user photos. The guard is Python-level, not a native-code sandbox. No other repository or historical test runner is required.
