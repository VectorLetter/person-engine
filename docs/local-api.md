# Local FastAPI service

The [Public V0 HTTP contract](public-v0-api-contract.md), [error table](public-v0-error-codes.md) and generated [OpenAPI](openapi.json) define the current wire contract. Apply responses include X-Apply-Outcome without changing the core receipt. Old job IDs remain JOB_NOT_FOUND after restart even if a request key is reused.

The `/v1` service wraps the frozen Public V0 core. `api/models.py` describes HTTP DTOs, `api/__init__.py` handles HTTP, and `api/service.py` routes workspaces and schedules jobs. Registration, identity, inference, matching/grouping, review, atomic apply and snapshots still execute the existing core functions. No core schema or semantics changed.

## Install and launch

Use CPython 3.12. JPEG/PNG use portable bounded OpenCV decoding without `sips`; Linux HEIC is unsupported. For Linux native use, install the OpenCV runtime system libraries documented in the [decode guide](linux-decode-compatibility.md), or use the supplied Docker image build. In a dedicated virtual environment:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-api-lock.txt
python -m pip install --no-build-isolation --no-deps -e .
person-engine-api --root ../local-api-workspaces
# Equivalent: python -m person_engine.api --root ../local-api-workspaces
```

The entry point binds **127.0.0.1:8000**, runs exactly one process, disables proxy trust and access logs, and holds a kernel lease on the dedicated service root. `--port` changes the port. No public-host option, account, authentication, remote upload, cloud store or production queue is provided. `/docs` and `/openapi.json` derive from the actual Pydantic endpoint models. Swagger UI assets use FastAPI's default external CDN; the JSON API itself has no CDN dependency.

A provider is optional. For real inference, supply `--model-config operator-model.json` containing the existing `LocalModels` constructor fields: detector/embedding paths, expected hashes, and explicit confidence/NMS/top_k. Model files are operator inputs, never uploaded or selected through HTTP. Alternatively construct `create_app(root, provider=LocalModels(...))`. For anonymous acceptance only, the operator may explicitly select `--synthetic-acceptance` with a dedicated disposable `--root`; see the [public synthetic acceptance setup](synthetic-acceptance.md). This option is mutually exclusive with `--model-config`, never a default or fallback, and cannot be selected through HTTP. Model availability means configured, not accuracy validated.

## Endpoint inventory

All JSON responses except `/health` and snapshot export use `{request_id, data}`. Request IDs also appear in `X-Request-ID`. Errors use `{code, message, request_id, details}`; details are currently null. Collection data uses `{items, total, offset, limit}` (default 100, maximum 512).

| Method | Path | Input / output |
| --- | --- | --- |
| GET | `/health` | Service liveness only: `{status: "ok"}` |
| POST | `/v1/workspaces` | WorkspaceCreate → WorkspaceView; 201 or core 409 |
| GET | `/v1/workspaces/{w}` | WorkspaceView, including current Basis |
| POST | `/v1/workspaces/{w}/assets` | AssetCreate → RegistrationView; 201 created, 200 replay, 409 conflict |
| POST | `/v1/workspaces/{w}/ingest-runs` | RunCreate → JobView; 202 queued, 200 process-local replay |
| GET | `/v1/workspaces/{w}/jobs/{j}` | JobView |
| GET | `/v1/workspaces/{w}/people` | Page of Person |
| GET | `/v1/workspaces/{w}/people/{p}` | Person |
| GET | `/v1/workspaces/{w}/faces` | Page of Face; optional asset_id filter |
| GET | `/v1/workspaces/{w}/assignments` | Page of Assignment; optional person_id filter |
| POST | `/v1/workspaces/{w}/reviews` | ReviewCreate → frozen ReviewView |
| GET | `/v1/workspaces/{w}/reviews/{r}` | ReviewView |
| GET | `/v1/workspaces/{w}/reviews/{r}/candidates` | Page of Candidate |
| POST | `/v1/workspaces/{w}/reviews/{r}/decisions` | DecisionSave → DecisionReceipt |
| POST | `/v1/workspaces/{w}/reviews/{r}/apply` | ApplyRequest → ApplyReceipt |
| POST | `/v1/workspaces/{w}/snapshots` | SnapshotCreate → SnapshotReceipt |
| GET | `/v1/workspaces/{w}/snapshots` | Page of SnapshotReceipt |
| GET | `/v1/workspaces/{w}/snapshots/{s}` | Public V0 SnapshotDocument |
| GET | `/v1/workspaces/{w}/snapshots/{s}/export` | Canonical Public V0 JSON attachment; hash equals receipt payload_sha256 |
| GET | `/v1/workspaces/{w}/diagnostics` | Diagnostics |

## Workspace and file binding

Create with `{"workspace_key":"example"}`. The key selects an opaque adapter directory; core assigns the returned `workspace_id`. The directory name is not the workspace ID. Routing uses core workspace IDs found only under the marked, operator-owned service root. The service refuses to adopt a nonempty unmarked directory. It never consults another repository or a historical workspace.

`created_at` is the immutable workspace marker file's modification timestamp, not a new core field. Backup tools that change this timestamp can change the reported value. `contract` carries the core schema contract; `api_version` is `v1`. `source_directory` is relative to the configured service root. Place authorized JPEG/PNG files in that directory out of band, then register `source_path` relative to it. No HTTP upload endpoint exists.

```json
{
  "source_namespace": "consumer",
  "source_ref": "photo-001",
  "source_path": "anonymous.png",
  "content_sha256": "<actual 64-character lowercase SHA-256>",
  "bytes": 1234,
  "media_type": "image/png",
  "expected_revision": 0
}
```

The API constructs only the existing core registration envelope (`contract`, routed workspace ID, key as core request_id). Logical source, metadata, expected revision, digest and bytes retain core semantics. The core verifies media headers and copies immutable content. An exact core receipt replay remains possible after the import file is removed. Even replays must pass lexical path/boundary validation.

Absolute paths, dot components, backslashes, symlink components, nonregular files and hardlinks are rejected. Other workspaces cannot be addressed through source_path. Root and import directories must be controlled by the local operator: this is not a sandbox against a hostile local process concurrently replacing filesystem components. Existing user-supplied display names remain user content; the service does not claim to anonymize them. Generated responses never add private filesystem paths, writer-owner records or embeddings.

Loopback Host and same-origin browser checks reject foreign hosts/origins. JSON request bodies are limited to 1 MiB. Unknown fields are rejected. No request/exception body logging is installed; the shipped launcher disables access logs. Request validation errors do not echo submitted values, paths or vectors. This is local isolation, not a multi-user authorization system; a trusted local caller can use any service-owned workspace ID.

## Jobs and consistency

RunCreate contains `asset_ids` (up to 512 unique registered IDs), `expected_basis` (the exact object returned by workspace GET), and explicit `policy` (`match_similarity`, `match_margin`, `cluster_similarity`). Empty asset_ids means match already ingested faces. No default thresholds or automatic register-all behavior exist.

A single in-process ThreadPool executor performs `core.ingest` for each asset and then `core.match`. State is QUEUED → RUNNING → SUCCEEDED, FAILED or BLOCKED. Progress is a stage, never a fabricated percentage. Success returns a core run_id for review creation. Missing provider returns MODEL_UNAVAILABLE; missing policy returns CALIBRATION_REQUIRED; both are BLOCKED outcomes. Stale basis or writer contention can also block a job. Other failures return safe diagnostics. Every job preserves PUBLIC_CALIBRATION_GAP.

Job polling reads only in-memory orchestration data, so inference cannot block it on a core database lock. Mutations on a busy workspace return 503 WRITER_BUSY; some domain reads may also encounter the core's single-writer/recovery boundary. There are at most 1024 jobs per service lifetime (including completed jobs); overflow returns 503 JOB_LIMIT. No eviction silently invalidates retry keys.

The executor is local-only, single-machine, and **not durable across restarts**. Graceful shutdown waits for queued jobs. Abrupt termination loses queued/running status; GET on an old job returns 404 after restart. Durable core receipts, facts, reviews and snapshots remain. Job retries are deduplicated only within one service lifetime. After restart, inspect current workspace basis and submit a new job key; there is no automatic recovery/resume. A multi-asset job is not a transaction: earlier asset ingests may remain committed if a later asset fails. Each ingest and review apply retain core atomicity.

## Review, decisions and apply

ReviewCreate accepts the completed core run_id. Core freezes workspace basis/source digest, source result and review revision. DecisionSave contains revision and a discriminated list of EXISTING_PERSON, NEW_PERSON, OUTSIDER, IGNORE_FACE, CORRECTION, REJECT or UNASSIGN decisions. Required target/expected-state fields follow the existing core contract. The adapter does not decide identity, interpret corrections or auto-enroll references.

ApplyRequest contains review revision. Core executes the entire apply transaction, source/revision checks, event audit and receipt. Same-key replay and duplicate apply with another key return the original receipt without changing the domain revision. Tests inject a failure after a human event inside core apply and verify rollback through HTTP reads before retrying.

## Idempotency and errors

| Mutation | Key handling |
| --- | --- |
| Workspace create | Stable workspace_key chooses the slot. Optional Idempotency-Key must equal it. Repeated create calls core initialize and returns WORKSPACE_EXISTS (409); core has no init replay receipt. No fictitious workspace replay store. |
| Asset register | Required Idempotency-Key maps directly to core register request_id; exact/BOUND replay returns 200, conflict 409. source_path is a validated transport binding, not part of core identity. |
| Run create | Required key deduplicates in-memory job scheduling; changed payload conflicts. Core ingest/match receive derived operation keys. No durable job guarantee. |
| Review create | Required key maps directly to core session request_id; replay returns frozen original result (201). |
| Decision submit | Required key maps directly to core save request_id; changed payload conflicts. |
| Apply | Required key maps directly to core apply request_id; duplicate apply also uses core no-op semantics. |
| Snapshot publish | Required key maps directly to core publish request_id; same payload returns original snapshot (201), changed payload conflicts. New key can publish a new version at the same basis, as core permits. |
| Snapshot export | GET invokes the existing immutable core export; no new key or publication. |

X-Request-ID is a validated correlation token or generated UUID hex. It is independent from Idempotency-Key. Mutations return their correlation ID and existing core receipt/revision or job reference. No separate business receipt database is introduced.

404: WORKSPACE_NOT_FOUND, JOB_NOT_FOUND, scoped NOT_FOUND. 409: ASSET_CONFLICT, CONFLICT, STALE_REVISION (also maps core STALE_SOURCE), frozen/correction/model-space/source conflicts. 422: malformed requests, INVALID_DECISION and unsafe sources. 503: WRITER_BUSY, JOB_LIMIT, model unavailable where surfaced synchronously. 500: sanitized INTERNAL_ERROR; job exceptions become RUN_FAILED. Python tracebacks and raw exception text are never client responses.

## Snapshots and diagnostics

Snapshot publication, listing, verification and immutable export expose only the Public V0 contract. No Organizer proposal/history or absolute local path fields are added. Export is the canonical core byte representation including its newline, and independently verifies against payload_sha256.

Diagnostics report schema, initialized state, provider configured/unavailable, latest in-memory job state, snapshot presence, PUBLIC_CALIBRATION_GAP, the legacy `LICENSE_UNDECIDED` token (retained for wire compatibility; source is now Apache-2.0, see [licensing](licensing.md)), unresolved AuraFace provenance and skipped real-model smoke. Health does not claim models or workspaces are ready.

## Validation

```sh
python -B scripts/run_release_tests.py --report ../core-tests.json
python -B scripts/run_api_tests.py --live --report ../api-tests.json
```

The API suite runs under the file/database/network test guard with an import allowlist. It creates fresh anonymous workspaces and injects the existing synthetic provider. Optional `--live` additionally starts an ephemeral loopback HTTP server, runs PUBLIC_V0_API_SYNTHETIC_E2E using httpx, then stops it. Live proof uses no external service/private fixture; it runs outside the no-network guard because it must bind loopback. Tests do not read private repositories, formal databases, models or actual photos.

Implementation references: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/) and [response models](https://fastapi.tiangolo.com/tutorial/response-model/).
