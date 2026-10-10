# Public V0 Local API Contract

API contract version: **v1 / Public V0 Local**. This document freezes the implemented Local V0 surface, not a remote service design. The machine-readable [OpenAPI](openapi.json) is generated from the actual FastAPI routes and Pydantic models. The [error table](public-v0-error-codes.md) is part of this contract. See [local startup](local-api.md) and the unchanged [Public V0 snapshot contract](snapshot-contract.md).

## Common wire rules

Base address: `http://127.0.0.1:8000` by default. `/v1` versions business endpoints; `/health` reports only process liveness. `/docs` and `/openapi.json` are schema/documentation facilities, not additional business resources. No SDK is generated.

JSON success envelopes are `{request_id, data}`. Exceptions are `/health` (`{status:"ok"}`) and snapshot export (the unchanged snapshot JSON attachment). Each response has `X-Request-ID`; supplied correlation tokens are validated, otherwise generated. JobView.request_id records the original submission's correlation ID while the envelope identifies the current request. Errors are `{code,message,request_id,details}`; details is currently null. Client logic must use code, not parse message.

IDs are opaque, case-sensitive tokens. Do not reconstruct IDs from hashes or directory names. Request tokens match `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`; digests are 64 lowercase hex characters. Unknown body fields are rejected. Explicit numeric revision values are nonnegative integers, not strings or booleans. Optional is distinct from nullable. Timestamps are RFC 3339 UTC with `+00:00`; null job timestamps mean the corresponding lifecycle event has not occurred. created_at for a workspace comes from its original marker's modification time; external restore tools may change that metadata.

The envelope/data shape and documented enum meanings are frozen. Clients should tolerate additive response fields. Removing/renaming fields, repurposing codes or changing business semantics requires a separately reviewed contract change. Existing core contract `person-engine-public-v0/1`, producer version, snapshot schema `person-facts/v0.1`, and HTTP `/v1` are different version domains. No v2 is designed here.

Every POST except workspace creation requires `Idempotency-Key`. It is not interchangeable with X-Request-ID. Retry with the same operation key and exact original semantic input; reading a newer basis and reusing an old key is a conflict. Token validity and body validation precede business execution. GET ignores Idempotency-Key and does not need one.

## Endpoint inventory

`{w}`, `{j}`, `{p}`, `{r}`, `{s}` below are workspace_id, job_id, person_id, review_id and snapshot_id in the generated OpenAPI. Common boundary errors apply to every endpoint: LOCAL_ONLY 403; REQUEST_TOO_LARGE 413; INVALID_REQUEST 422; HTTP_ERROR 400/405/415 when applicable; sanitized INTERNAL_ERROR 500. Scoped domain reads can return WRITER_BUSY 503. Endpoint-specific principal errors are listed separately.

| Method | Path | Purpose | Read/mutation | Idempotency | Main response | Main errors |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/health` | Service liveness only; does not assert workspace or model readiness | Read | Read only | 200 Health | None beyond common boundary errors |
| POST | `/v1/workspaces` | Create a fresh local workspace | Mutation | Stable workspace_key; duplicate conflicts | 201 Envelope_WorkspaceView_ | WORKSPACE_EXISTS, INVALID_REQUEST |
| GET | `/v1/workspaces/{workspace_id}` | Read workspace identity, current source basis and the relative local import directory | Read | Read current basis | 200 Envelope_WorkspaceView_ | WORKSPACE_NOT_FOUND, INVALID_WORKSPACE |
| POST | `/v1/workspaces/{workspace_id}/assets` | Register a logical source binding using a workspace-relative local file | Mutation | Required key; created/replay/conflict | 201 Envelope_RegistrationView_, 200 Envelope_RegistrationView_ | ASSET_CONFLICT, STALE_REVISION, UNSAFE_PATH, SOURCE_CHANGED, UNSUPPORTED_MEDIA |
| POST | `/v1/workspaces/{workspace_id}/ingest-runs` | Queue local ingest followed by non-authoritative matching | Mutation | Required key; in-process same job replay | 202 Envelope_JobView_, 200 Envelope_JobView_ | CONFLICT, WORKSPACE_MISMATCH, NOT_FOUND, JOB_LIMIT |
| GET | `/v1/workspaces/{workspace_id}/jobs/{job_id}` | Read ephemeral job lifecycle and stage progress | Read | Read current ephemeral state | 200 Envelope_JobView_ | JOB_NOT_FOUND, WORKSPACE_NOT_FOUND |
| GET | `/v1/workspaces/{workspace_id}/people` | Read human-created people with offset/limit pagination; no implicit machine identities | Read | Read current projection | 200 Envelope_Page_Person__ | WORKSPACE_NOT_FOUND |
| GET | `/v1/workspaces/{workspace_id}/people/{person_id}` | Read one human-created person scoped to this workspace | Read | Read current projection | 200 Envelope_Person_ | NOT_FOUND, WORKSPACE_NOT_FOUND |
| GET | `/v1/workspaces/{workspace_id}/faces` | Read public face geometry and human state, optionally filtered by asset_id | Read | Read current projection | 200 Envelope_Page_Face__ | WORKSPACE_NOT_FOUND |
| GET | `/v1/workspaces/{workspace_id}/assignments` | Read current human-confirmed face/person assignments, optionally filtered by person_id | Read | Read current projection | 200 Envelope_Page_Assignment__ | WORKSPACE_NOT_FOUND |
| POST | `/v1/workspaces/{workspace_id}/reviews` | Freeze a core run into a review bound to workspace source basis and review revision | Mutation | Required key; original creation result | 201 Envelope_ReviewView_ | NOT_FOUND, STALE_REVISION, CONFLICT |
| GET | `/v1/workspaces/{workspace_id}/reviews/{review_id}` | Read the current review | Read | Read current review | 200 Envelope_ReviewView_ | NOT_FOUND |
| GET | `/v1/workspaces/{workspace_id}/reviews/{review_id}/candidates` | Read frozen review candidates | Read | Read frozen source candidates | 200 Envelope_Page_Candidate__ | NOT_FOUND |
| POST | `/v1/workspaces/{workspace_id}/reviews/{review_id}/decisions` | Save a complete decision list as a new draft revision using core validation | Mutation | Required key; original save receipt | 200 Envelope_DecisionReceipt_ | INVALID_DECISION, STALE_REVISION, FROZEN_REVISION, CORRECTION_REQUIRED, CONFLICT |
| POST | `/v1/workspaces/{workspace_id}/reviews/{review_id}/apply` | Atomically apply the specified review revision | Mutation | Required key; duplicate apply also no-op | 200 Envelope_ApplyReceipt_ | STALE_REVISION, CONFLICT, INVALID_DECISION, INTERNAL_ERROR |
| POST | `/v1/workspaces/{workspace_id}/snapshots` | Publish an immutable versioned Public V0 snapshot from expected_basis | Mutation | Required key; same receipt/version | 201 Envelope_SnapshotReceipt_ | STALE_REVISION, CONFLICT, SNAPSHOT_INVALID |
| GET | `/v1/workspaces/{workspace_id}/snapshots` | List publication receipts ordered by increasing snapshot_version with offset/limit pagination | Read | Read publication list | 200 Envelope_Page_SnapshotReceipt__ | WORKSPACE_NOT_FOUND |
| GET | `/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}` | Read and verify one immutable Public V0 snapshot | Read | Immutable publication | 200 Envelope_SnapshotDocument_ | NOT_FOUND, SNAPSHOT_INVALID |
| GET | `/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/export` | Download canonical Public V0 snapshot JSON with trailing newline | Read + idempotent local materialization | Immutable bytes; no publication | 200 SnapshotDocument | NOT_FOUND, SNAPSHOT_INVALID, CONFLICT, UNSAFE_PATH |
| GET | `/v1/workspaces/{workspace_id}/diagnostics` | Read local workspace readiness and known limitations | Read | Read current readiness | 200 Envelope_Diagnostics_ | WORKSPACE_NOT_FOUND, INVALID_WORKSPACE |

## Resource identities, versions and lifecycles

| Resource | Identity / relationship | Revision, authority and mutability |
| --- | --- | --- |
| Workspace | Stable workspace_id from core; owns every resource below | basis.revision/source_digest identify current source facts. Workspace creation itself is not replayable. |
| Asset | Stable asset_id from workspace + source_namespace/source_ref; content digest is separate | Binding and registration receipt are immutable. Equal content does not imply equal logical asset. |
| Job | Ephemeral opaque job_id scoped to a workspace and server lifetime | Mutable lifecycle only; not a source revision. No executor/future/thread is part of the wire contract. |
| Run | Durable opaque run_id in a successful job's result_refs | Immutable machine result at a source basis/policy; scores/groups never grant identity. No independent run GET endpoint in this contract. |
| Face | Stable asset-scoped face_id with detection_id/model_space as provenance | Detection geometry is immutable; projected human state can change through apply. |
| Person | Stable person_id, human-created | display_name/status are projected facts; IDs do not change on rename/retirement through core. API offers reads and review-based creation, not new CRUD endpoints. |
| Assignment | Current relation keyed by face_id, with person_id/event_id | Human-confirmed projection, not an independent assignment ID. Removed/replaced by later reviewed actions; historical events stay append-only. |
| Review | URL review_id is the returned session_id | DRAFT revisions append; current draft advances; applied revision freezes. source/basis/candidates are immutable for the session. |
| Decision | A face decision inside a review revision; no standalone decision_id | POST replaces the complete draft decision list by appending a new revision; it does not merge with the previous draft. |
| Apply Receipt | Stable application_id, session_id, revision, receipt_digest | Immutable; duplicate apply reuses it. event_ids are immutable audit references; domain_revision is the resulting source revision. |
| Snapshot | Stable snapshot_id with workspace-local increasing snapshot_version | Immutable full publication, source_digest, schema/producer versions and fixed timestamp. Exports preserve canonical bytes. |

Clients never need a database table name. `session_id` remains the core receipt field and **means the review resource ID**; changing it would rewrite receipt JSON/digests. `registry_revision` and `domain_revision` are core source revisions at the recorded operation, not separate clocks clients should advance themselves. Always use the complete latest workspace basis for a new run or publication.

## Workspace and authorized local source

WorkspaceCreate has one required field: workspace_key. It is an opaque local creation slot; core chooses workspace_id. Reusing a key invokes the existing core initialize rule and returns WORKSPACE_EXISTS, with no directory overwrite. An optional creation Idempotency-Key must equal workspace_key. Keep the successful workspace_id response: there is no workspace listing or key lookup recovery endpoint in this V0 surface.

WorkspaceView exposes identity, created_at, state INITIALIZED, contract, api_version, basis, and source_directory. No DB path, private absolute root, writer-owner metadata or state/content/export directory paths are exposed. The single relative `source_directory` is necessary for the path-based capability: the authorized local operator places files there out of band, relative to the configured service root. Treat it as an opaque placement hint; do not parse or depend on its current slot layout.

AssetCreate requires source_namespace, source_ref, source_path, content_sha256, bytes, media_type, expected_revision. The source pair is logical identity within a workspace. The digest identifies bytes; registering identical bytes under two source references creates two assets. A source reference cannot be rebound to different bytes/type/size. source_path is transport binding only and is not added to core identity. Extra metadata fields are not accepted; current metadata is digest, byte count and JPEG/PNG media type.

Allowed root is this workspace's import directory only. source_path is relative to it; absolute paths, dot/dot-dot/empty components, backslashes, symlink components, nonregular files and hardlinks are rejected. No file-selection or arbitrary-root escape mechanism exists. A newly registered missing file returns SOURCE_CHANGED 409. Exact receipt replay and a matching already-bound logical source can succeed after the import file is removed, because core already owns the content; safe lexical path validation still applies. Core verifies actual size, digest and media signature for new bytes.

This is Local V0 path binding, **not remote upload**. The operator controls the root and filesystem. Traversal/cross-workspace checks are not a sandbox against a malicious local process swapping components concurrently. Local callers with workspace IDs are trusted; no accounts/auth or per-user ownership is claimed.

## Job and run contract

RunCreate requires asset_ids and expected_basis; policy may be omitted/null to produce CALIBRATION_REQUIRED. An explicit policy has match_similarity, match_margin and cluster_similarity, without defaults. asset_ids must be unique registered IDs in the routed workspace; an empty list requests matching on already-ingested faces.

QUEUED → RUNNING → SUCCEEDED / FAILED / BLOCKED. created_at is always set; started_at and finished_at are null until reached. `progress` is only queued/ingest/match/complete/stopped; there is no percent or timing promise. SUCCEEDED has result_refs `{run_id}`. Other states have an empty result_refs object; terminal failure/block carries ErrorView. Diagnostics retain PUBLIC_CALIBRATION_GAP. Missing provider and missing policy are BLOCKED; stale source and writer contention can also block. A GET of a failed job still returns HTTP 200; error.code describes the job, not the GET's HTTP status.

Known limitation: jobs and job-key deduplication are in memory. A server restart drops all old job states. For an existing workspace, an old job ID **always returns 404 JOB_NOT_FOUND**, including after a new submission reuses the same key. New service lifetime produces a new opaque job ID. No automatic resume, restore or persistent queue exists. If the workspace itself is absent, WORKSPACE_NOT_FOUND takes precedence. Clients must inspect current facts/basis after interruption and submit a new operation deliberately.

Within one lifetime, the same key/body returns the same job and current state (200), even if it failed; changed body conflicts. Jobs are capped at 1024 per lifetime. Graceful shutdown drains the executor; abrupt termination can leave earlier asset ingests committed. A multi-asset job is not atomic as a whole. Each core ingest is atomic, then matching creates a durable core run.

## People, faces and assignments

Pagination is offset ≥ 0 and limit 1–512, default 100, with items/total/offset/limit. Filters are exact opaque `asset_id` on faces and `person_id` on assignments. Unknown valid filters return empty collections. Existing resource reads are workspace-scoped. Lists are deterministic at a fixed state, but offset pagination is not a cross-request snapshot; concurrent mutations can change total/order. Use published snapshots for a consistent consumer read.

UNKNOWN means no human terminal fact; ASSIGNED is human-confirmed identity; OUTSIDER is a face outside the roster; IGNORED is a human false-detection decision; UNASSIGNED is explicit human removal. A machine candidate never changes these facts. Face geometry.score is detector output, not identity confidence. CandidateScore.similarity is uncalibrated cosine evidence (floating-point roundoff may apply), not probability. Candidate scores/order/grouping are fixed within a frozen run, but not promised stable across future runs, providers or policy configurations. They are intentionally exposed only for review; raw embeddings and private calibration internals are not exposed.

## Review and decision schema

ReviewCreate requires run_id. Core checks that run's workspace basis is current, then freezes source/basis and starts revision 0, DRAFT. POST creation replay returns its original creation response (which may be older than current review state); GET is authoritative for the current draft/applied state. Candidates expose immutable run provenance, non-authoritative comparison evidence and the frozen human state. basis contains workspace_id/revision/source_digest. No review file path appears.

DecisionSave requires revision and a nonempty list of at most 512 decisions. Every decision requires face_id, action and expected_state. expected_state requires state plus person_id (nullable): ASSIGNED requires a person ID; every other state requires null. The exact state must agree with the frozen candidate. Each face appears once and must belong to the review.

| action | Additional required fields | Optional fields | Invalid combinations / core conditions |
| --- | --- | --- | --- |
| EXISTING_PERSON | person_id | enroll_reference boolean | Target must be active; expected state UNKNOWN/UNASSIGNED; a prior rejection needs explicit CORRECTION. |
| NEW_PERSON | display_name, person_key | enroll_reference boolean | Expected UNKNOWN/UNASSIGNED. Name trimmed, 1–128 chars, no controls, unique. Same person_key may group faces with the same name; inconsistent key/name grouping conflicts. |
| OUTSIDER | none | enroll_reference=false | enroll_reference=true invalid; no target person fields. |
| IGNORE_FACE | none | enroll_reference=false | enroll_reference=true invalid; no target person fields. |
| UNASSIGN | none | enroll_reference=false | enroll_reference=true invalid; no target person fields. |
| REJECT | person_id | enroll_reference=false | Target active; cannot enroll. Core records rejection; may retain another existing state or unassign the rejected identity. |
| CORRECTION | to = HumanState | enroll_reference boolean | to.UNKNOWN invalid; to.ASSIGNED needs active person; non-assigned target requires null and cannot enroll. |

`enroll_reference` is optional, strictly boolean when supplied, **never null**. Omission is passed through as omission; explicit false stays explicit false. Unknown/irrelevant fields are rejected. Structural decision-body failures and core semantic failures return INVALID_DECISION; missing/invalid headers still return INVALID_REQUEST. Cross-field state validity, roster membership, corrections and identity semantics remain core-owned, not duplicated in Pydantic. Save appends a draft revision; it does not apply facts.

## Apply contract

ApplyRequest is `{revision}`. It names a saved review revision. Successful apply commits all human facts/events/person creation and the receipt atomically. A failure rolls them back; a safe INTERNAL_ERROR 500 may be retried using the same key. Metadata about the writer lease is not a domain fact.

| Client outcome | HTTP / discriminator | Receipt and domain state |
| --- | --- | --- |
| SUCCESS | 200, X-Apply-Outcome: SUCCESS | First application; original core receipt returned. |
| IDEMPOTENT_REPLAY | 200, X-Apply-Outcome: IDEMPOTENT_REPLAY | Exact key replay or a new key applying the already-applied same revision; same receipt, no new domain revision/events. |
| STALE_CONFLICT | 409, STALE_REVISION / CONFLICT / FROZEN_REVISION or related conflict code | No partial application. A changed request under the same key conflicts. |
| VALIDATION_ERROR | 422, INVALID_REQUEST / INVALID_DECISION or documented core validation code | No partial application. |
| INTERNAL_FAILURE | 500, INTERNAL_ERROR | Safe message; transaction rollback. |

The HTTP outcome is metadata only; it does not alter the core ApplyReceipt or receipt_digest. Duplicate apply remains a no-op even after subsequent unrelated domain changes, as core's applied-receipt branch precedes source revalidation for the same revision. A different revision still conflicts.

## Snapshot and export contract

SnapshotCreate requires expected_basis. Publish returns SnapshotReceipt with snapshot_id, snapshot_version, schema_version, source_digest, content_digest, payload_sha256 and published_at. The immutable document additionally carries producer_version, generated_at, persons/faces/assignments/ignored_state/provenance/compatibility. Those fields follow the existing person-facts/v0.1 contract exactly. No adapter-specific job, filesystem, Organizer, proposal/history field is inserted.

source_digest identifies publication source facts, content_digest hashes canonical document JSON, payload_sha256 hashes canonical UTF-8 bytes plus newline. Snapshot version increases per workspace; it is distinct from source and review revisions. Exact publication replay returns the original version/time/receipt. New key may create a new publication at unchanged source basis. Older snapshots remain immutable.

Export GET verifies publication and may create/verify its immutable local export file; the response itself contains the canonical JSON attachment, never a filesystem location. Repeated export returns identical bytes and creates no new snapshot. A conflicting existing export is CONFLICT 409, never overwritten. Snapshot integrity failure is SNAPSHOT_INVALID 500. Read and export share the same Public V0 document; no new snapshot schema is introduced. Names supplied by consumers remain user content, not anonymized data.

## Idempotency matrix

| Mutation | Key scope / requirement | Exact retry | Changed request / new operation | Receipt / revision effects |
| --- | --- | --- | --- | --- |
| Workspace create | workspace_key; optional header must match | 409 WORKSPACE_EXISTS | Different key creates fresh workspace; no overwrite | No init replay receipt. |
| Asset register | Required header; workspace + register operation | 200 REPLAY, original receipt | Same key changed core payload → 409 ASSET_CONFLICT; new key+same logical binding → 200 BOUND_REPLAY | Exact retry no revision; bound replay has its own request receipt but same asset/registration ID. |
| Run create | Required header; workspace + server lifetime | 200 same job/current state | Changed body → CONFLICT; after restart a new job ID | Scheduling state only; core ingest/match operations use derived keys. |
| Review create | Required header; workspace + session operation | 201 original frozen creation result | Changed run_id → CONFLICT | No new review/revision on retry. |
| Decision save | Required header; workspace + save operation | 200 original DecisionReceipt | Changed session/revision/decision list → CONFLICT | No draft revision on retry; successful new save appends one. |
| Apply | Required header; workspace + apply operation | 200 original receipt, IDEMPOTENT_REPLAY | New key same applied revision is also replay; changed payload under one key conflicts | No new domain revision/event/person on replay. |
| Snapshot publish | Required header; workspace + publish operation | 201 original SnapshotReceipt | Same key changed basis → CONFLICT; new key can create next snapshot version | Replay never republishes. |
| Export GET | No key; snapshot identity | 200 identical canonical bytes | Existing incompatible output → CONFLICT | Materializes immutable export only; no domain revision. |

The core, not HTTP, defines logical identity and business receipt semantics. source_path is excluded from core registration payload, while all registration identity/metadata/revision fields are preserved. Body omission versus explicit optional fields can change a core request digest; preserve original input for retries.

## Schema review and known limitations

Every declared DTO, including abstract/base DTOs, was inspected for required/nullable fields, IDs, enum values, timestamp shape, envelope consistency and internal fields. Generic Envelope/Page schema names in OpenAPI are intentional concrete instantiations, not duplicate domain resources. CandidateScore and ProvisionalGroup are explicit models; the nested Public V0 snapshot provenance/compatibility dictionaries remain governed by the existing snapshot contract instead of inventing a competing adapter schema. Some semantic rules depend on frozen source/person state and deliberately remain in core validation.

Known limitations: localhost only; operator-owned path-based local sources; no upload; no auth; single-machine in-memory jobs; no job restart durability; no run-list/recovery API; bounded matching and job count; PUBLIC_CALIBRATION_GAP; real-model validation incomplete. Project source uses Apache-2.0; model weights retain separate terms and AuraFace redistribution provenance remains unresolved. The legacy diagnostic token `LICENSE_UNDECIDED` is retained for wire compatibility; see [licensing](licensing.md). Only synthetic validation is asserted. No new features, persistent queue, calibration or production readiness are implied.
