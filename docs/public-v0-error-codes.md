# PUBLIC_V0_ERROR_CODE_TABLE

Version: **v1 / Public V0 Local**. Generated code/status/message inventory comes from `person_engine.api.errors.PUBLIC_V0_ERROR_CODE_TABLE`; meanings are reviewed against the actual core call sites.

Wire shape: `{code, message, request_id, details}`. details is currently null. Message text is safe explanation, not a parsing interface. Unknown exception codes and exception text cannot pass through. Job errors use the same code vocabulary inside a 200 JobView response; the status column describes synchronous HTTP mapping, not job GET status.

| code | HTTP | Meaning / handling | Safe message |
| --- | --- | --- | --- |
| `ASSET_CONFLICT` | 409 | Existing logical binding or registration key has different semantics; correct request/key, never overwrite. | Registration conflicts with an existing binding or request. |
| `CALIBRATION_REQUIRED` | 422 | Job policy was omitted/null; job is BLOCKED. No thresholds are guessed. | Explicit candidate policy is required. |
| `CONFIG_REQUIRED` | 422 | Core requires valid explicit provider/candidate parameters; unlike absent job policy, this is core validation. | The core contract rejected this operation. |
| `CONFLICT` | 409 | Existing operation key/output/name conflicts. Exact retry must preserve its original payload. | Request conflicts with existing core state. |
| `CONTENT_LIMIT` | 422 | Size, pixel, detection or matching bound exceeded. | The core contract rejected this operation. |
| `CORRECTION_REQUIRED` | 409 | Changing an existing human fact/rejection requires an explicit correction decision. | The core contract rejected this operation. |
| `DECODE_FAILED` | 422 | Bounded local decoder rejected image; decoder stderr/path is not exposed. | The core contract rejected this operation. |
| `DETECTION_INVALID` | 422 | Provider emitted invalid detection geometry/score. | The core contract rejected this operation. |
| `EMBEDDING_INVALID` | 422 | Provider emitted invalid vector shape/norm/finiteness; raw vector is never returned. | The core contract rejected this operation. |
| `FROZEN_REVISION` | 409 | The review has already been applied and its draft is immutable. | The core contract rejected this operation. |
| `HTTP_ERROR` | 400 / 405 / 415 (framework) | Framework request rejection, normally method 405; preserve framework 400/405/415 when applicable. | HTTP request rejected. |
| `INCOMPATIBLE_REQUEST` | 422 | Core registration contract version is unsupported (reserved propagation). | The core contract rejected this operation. |
| `INCOMPATIBLE_WORKSPACE` | 422 | Stored workspace contract is not the supported fresh Public V0 contract. | The core contract rejected this operation. |
| `INTERNAL_ERROR` | 500 | Unexpected synchronous failure/unknown error code; raw exception is hidden. | The operation failed. |
| `INVALID_DECISION` | 422 | Decision body shape or core semantic constraints rejected; no fact mutation. | The decision does not satisfy the core contract. |
| `INVALID_REQUEST` | 422 | Malformed schema/JSON/header/identifier, unexpected field or invalid operation input. | The request does not satisfy the API contract. |
| `INVALID_WORKSPACE` | 422 | Core workspace resources are missing or unsafe; operator must inspect local workspace. | The core contract rejected this operation. |
| `JOB_LIMIT` | 503 | 1024-job in-memory lifetime limit reached; operator action required, no silent eviction. | The in-memory job limit has been reached. |
| `JOB_NOT_FOUND` | 404 | Unknown job, foreign workspace job, or a job from a prior server lifetime. | Job not found. |
| `LOCAL_ONLY` | 403 | Foreign Host/Origin rejected by local boundary. | Only same-origin loopback requests are supported. |
| `MODEL_CHANGED` | 422 | Configured model bytes no longer match expected hashes. | The core contract rejected this operation. |
| `MODEL_CONTRACT` | 422 | Provider manifest violates the current core provider contract. | The core contract rejected this operation. |
| `MODEL_SPACE_CONFLICT` | 409 | Workspace already pins a different model/compute space. | The core contract rejected this operation. |
| `MODEL_UNAVAILABLE` | 503 | Provider absent; ingest job is BLOCKED. No downloads occur. | No local model provider is configured. |
| `NOT_FOUND` | 404 | Resource does not exist in the routed workspace; also unknown route. | Resource not found in this workspace. |
| `REQUEST_TOO_LARGE` | 413 | HTTP body exceeds 1 MiB. | Request body exceeds the local API limit. |
| `RUN_FAILED` | 500 | Unexpected job failure, including unknown provider error codes; inspect facts for committed earlier ingests. | Local job failed. Inspect local operator diagnostics. |
| `SNAPSHOT_INVALID` | 500 | Stored snapshot integrity validation failed; operator investigation, not client correction. | Published snapshot integrity validation failed. |
| `SOURCE_CHANGED` | 409 | New source is missing, has wrong size/digest, or registered bytes changed. | The core contract rejected this operation. |
| `STALE_REVISION` | 409 | Core STALE_SOURCE and STALE_REVISION normalize here. Refresh source/review state; do not blindly reuse the old key with changed body. | The supplied revision or source basis is stale. |
| `UNSAFE_PATH` | 422 | Path violates the scoped local source/output boundary. | Source must be a regular file inside this workspace import directory. |
| `UNSUPPORTED_MEDIA` | 422 | Registered media type/signature is not supported JPEG/PNG. | The core contract rejected this operation. |
| `WORKSPACE_EXISTS` | 409 | The creation slot already contains a workspace; core initialization forbids overwrite. | Core initialization requires a new or empty workspace. |
| `WORKSPACE_MISMATCH` | 409 | Supplied source basis is from another workspace or routed identity changed. | The core contract rejected this operation. |
| `WORKSPACE_NOT_FOUND` | 404 | Unknown service workspace ID. | Workspace not found. |
| `WRITER_BUSY` | 503 | A workspace/service writer is active; retry later with same exact request if it was not accepted. | Workspace writer is busy. |

Aliases: core STALE_SOURCE → STALE_REVISION; core CONFLICT in registration → ASSET_CONFLICT; unlisted FoundationError or unexpected exception → INTERNAL_ERROR (synchronous) / RUN_FAILED (job). Other listed core codes retain their meaning. No traceback, source value, decoder stderr, embedding or private path enters details.

Structural decision body errors normalize to INVALID_DECISION, while missing/invalid headers remain INVALID_REQUEST. Error ordering follows boundary → request schema → scoped lookup/core validation; clients must not depend on precedence when multiple independent defects exist. Authentication/authorization error codes are not part of this local contract.
