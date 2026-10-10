# Docker V0 runtime

Single user, single machine, one service process per dedicated workspace root.
This is a local acceptance runtime, with no authentication, upload API, distributed
queue, multi-writer support or production readiness claim.

## Configure and start

Start from a fresh source checkout. For the shortest no-model verification path,
use the [synthetic quickstart](../README.md#quickstart-synthetic-docker-acceptance).
The configuration below uses the real-provider default and requires operator-supplied
models for inference; health can pass with no models configured.

```sh
cp .env.example .env
mkdir -p runtime/workspace runtime/photos runtime/models
# Linux bind mounts retain ownership. Run as your non-root host identity.
export PERSON_ENGINE_UID="$(id -u)" PERSON_ENGINE_GID="$(id -g)"
# Edit the three host directory paths, provider mode and host port in .env.
docker compose build
docker compose up -d
curl http://127.0.0.1:8000/health
docker compose logs --follow
```

Use fresh, dedicated workspace directories, writable by the configured UID/GID.
Photo/model directories must already exist and be readable. Compose uses
`127.0.0.1:8000:8000`; changing the host port does not change container port 8000.
Never expose this unauthenticated service on a public interface. The image's
non-root default is UID/GID 10001. Root filesystem is read-only in Compose;
application state, home, temporary files and caches live on the workspace volume.
Do not run a second service against that directory.

| Host input | Container path | Access |
| --- | --- | --- |
| Workspace directory | `/data/person-engine` | read/write |
| Original photo directory | `/media/photos` | read-only |
| Operator model directory | `/models` | read-only |

The dedicated API root is `/data/person-engine/service`. Runtime caches live in
`/data/person-engine/runtime`, outside the marker-protected API root. Each workspace
has its own imports directory. Do not mix a native core workspace or private
service directory into this layout. Native `python -m person_engine.api` still
binds `127.0.0.1` and uses its existing CLI options. Container environment variables
are translated only by `person_engine.container_runtime`.

## Explicit provider configuration

The default `PERSON_ENGINE_PROVIDER=real` never selects synthetic automatically.
An empty model mount starts the API with `MODEL_UNAVAILABLE` and
`PUBLIC_CALIBRATION_GAP` diagnostics. `/health` and Docker healthcheck only test
process liveness. Diagnostics are per workspace:
`GET /v1/workspaces/{workspace_id}/diagnostics`.

For externally supplied, operator-approved real models, place `provider.json` in
the read-only model mount. It takes the existing `LocalModels` keyword arguments:
`detector_path`, `embedding_path`, `detector_sha256`, `embedding_sha256`,
`confidence`, `nms`, `top_k`. Paths must stay within `/models`; relative paths are
resolved there. Supply your own expected hashes and explicit detector parameters.
Missing configuration means unavailable; present but invalid configuration fails
startup with a safe error and never falls back. This source release supplies no weights,
model downloads, calibration values or real-model validation.

**TEST / ACCEPTANCE ONLY:** synthetic acceptance requires explicit `PERSON_ENGINE_PROVIDER=synthetic_acceptance`.
Use only anonymous fixture media and a disposable dedicated workspace for it.
Diagnostics explicitly say synthetic/test mode. It cannot close the public
calibration gap or establish real-person identification quality.

## Explicit import, then unchanged HTTP API

Create a workspace through `POST /v1/workspaces` with a fresh `workspace_key`.
Keep its returned `workspace_id` and opaque `source_directory`. Pass the latter
unchanged to the helper; do not derive it from an ID.

```sh
# Set SOURCE_DIRECTORY to the exact source_directory returned by the API.
docker compose exec -T person-engine python -m person_engine.import_tool \
  --root /data/person-engine/service \
  --source-directory "$SOURCE_DIRECTORY" \
  --source-root /media/photos --source album/example.jpg
```

Native use has the same interface:

```sh
python -m person_engine.import_tool --root "$SERVICE_ROOT" \
  --source-directory "$SOURCE_DIRECTORY" \
  --source-root "$PHOTO_ROOT" --source album/example.jpg
```

The helper opens sources read-only, copies their bytes without conversion and
atomically publishes a new destination without overwriting an existing file.
It requires existing API/workspace markers; it cannot initialize or adopt a
workspace. Traversal, symlink components and destination hard links are rejected.
Nested source paths are permitted; destination directories are created only under
that workspace's imports. No database is opened and no asset is registered by the
helper. Files are bounded to the existing 64 MiB media input limit.

The default destination is the content SHA-256 plus the magic-detected extension.
`--destination nested/name.jpg` sets an explicit imports-relative name. A JSON
receipt contains `source_reference`, `source_path`, `content_sha256`, `bytes`,
`media_type`, `status`, with no host absolute path. Exit 0 means `COPIED` or
`REUSED`; exit 3 means `CONFLICT` (different bytes, destination unchanged); exit 2
is a safe path/input/I/O error. Source bytes, size and modification time are
preserved. Access time is not part of the preservation guarantee. A process crash
can leave a hidden `.import-*` temporary copy; it never replaces or deletes the
original source or an existing destination. A complete copy still needs API
registration and content validation.

Register using the receipt's `source_path`, digest, size and media type, plus the
existing namespace, logical source reference, expected revision and idempotency
key required by the [frozen API](public-v0-api-contract.md). If the receipt says
`example.jpg`, send `example.jpg`, **not** `/media/photos/example.jpg` or
`imports/example.jpg`. The returned directory is already the imports boundary;
adding `imports/` again selects a nested directory. Neither native nor Docker
changes that contract. Continue with ingest, review, decisions, apply and snapshot
through the existing `/v1` endpoints.

## Anonymous acceptance

Requires Docker Compose V2/BuildKit and a separate Python environment with
`httpx==0.28.1` and `jsonschema==4.26.0`, without person-engine installed. Keep this
client environment and evidence outside the repository. For example, create a
venv with `python3 -m venv "$CLIENT_ENV"`, then install those two pinned packages
with `"$CLIENT_ENV/bin/python" -m pip install httpx==0.28.1 jsonschema==4.26.0`.
The runner uses an ephemeral localhost port and new temporary paths rather than
any configured personal `.env` data.

```sh
python3 scripts/docker_acceptance.py \
  --client-python "$CLIENT_ENV/bin/python" \
  --output "$NEW_EMPTY_EVIDENCE_DIRECTORY"
```

It builds the image, generates uniform anonymous JPEG/PNG pixels, proves the
empty-model default, explicitly selects synthetic, imports using the CLI, runs a
separate HTTP-only consumer against the frozen OpenAPI schema, and removes/recreates
the container with the same bind mount. The independent client forbids service
imports and workspace/database reads. It verifies all 20 endpoint successes,
registration and apply replay, review decisions, stale/conflict/error responses,
persistent facts/snapshot/export, old-job `JOB_NOT_FOUND`, and unchanged source
file set/size/hash/mtime. It also attempts writes on both read-only mounts and
checks actual port bindings, installed module bytes and Docker health status.
Containers/networks created by the runner are removed; local evidence remains.

Use `scripts/scan_docker_image.py` to audit the saved final image's layers and
filesystem artifact names/magic and sensitive patterns. Pattern hits require
review: `--review` accepts a local JSON list of exact `path`, `kind`, `sha256` and
nonempty `reason` entries. Raw findings remain in the report. Use this only after
checking upstream provenance (for example official base icons or public library
self-test vectors); unmatched content remains blocked. The image retains required
runtime code and public docs, but excludes tests/evidence, user media/state,
external model weights, Git history and local configuration. Dependency example
models, OpenCV cascade data and dependency test fixtures are removed in the same
installation layer. BuildKit download caches are outside image layers.

## Persistence and limits

Stop/remove/recreate preserves workspace registry, review receipts and published
snapshots on the bind mount. Jobs are intentionally in memory: old job IDs return
`JOB_NOT_FOUND` in a new process. Do not infer durable queued work from a retained
workspace or try to restore jobs by reading its DB.

JPEG and PNG are supported through byte-signature routing, regardless of suffix.
Actual HEIC is rejected by the helper as `UNSUPPORTED_MEDIA_JPEG_PNG_ONLY`;
HEIC Linux support remains pending. No conversion or Linux `sips` invocation is
provided. Existing API decode errors remain unchanged if imports are populated by
another explicit operator process.

The pinned Python base is a multi-architecture index. Linux arm64 is the acceptance
runtime; linux/amd64 remains **METADATA_ONLY**, not runtime-verified. System packages
come from the configured Debian repository, so their resolved versions are not a
bit-for-bit build lock. Python dependency versions remain frozen.

Source license is **Apache-2.0**; dependency licenses remain separate. AuraFace is
**EXTERNAL_ONLY / USER_SUPPLIED / REDISTRIBUTION_NOT_VERIFIED**,
**PUBLIC_CALIBRATION_GAP** open, and real-model validation incomplete. There is no
authentication, remote upload, persistent queue, model redistribution or SaaS
support. Host exposure is localhost only and input remains local import-relative.
