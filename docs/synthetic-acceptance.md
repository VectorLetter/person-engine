# Independent synthetic acceptance provider

**TEST / ACCEPTANCE ONLY.** This is a deterministic fixture interpreter, not a
face model. It never validates real-model accuracy or calibration, creates no
human identity without review/apply, and never downloads or loads model weights.

## Public activation

Use the installed `person-engine-api` entry point from [local setup](local-api.md):

```sh
acceptance_root="$(mktemp -d)/service-root"
person-engine-api --root "$acceptance_root" --port 8000 --synthetic-acceptance
```

The operator must retain that root for file placement and a restart test. Use a
new disposable root; never point it at production, personal or real-model data.
Restart with the same root and flag. The process still binds only 127.0.0.1.

`--synthetic-acceptance` requires an explicit `--root` and is mutually exclusive
with `--model-config`. Without either provider option the existing
MODEL_UNAVAILABLE behavior remains. A bad real-model config fails startup; it
does not activate synthetic inference. The native CLI has no environment-variable fallback or HTTP provider selector.
Docker has a separate explicit `PERSON_ENGINE_PROVIDER=synthetic_acceptance`
opt-in; it never falls back from real models. See the [Docker guide](docker-runtime.md). The independent client needs no service import,
Python path change, fixture hook or internal script.

## Diagnostics within the frozen schema

GET workspace diagnostics retains `model_availability: CONFIGURED` (configured
does not mean real model) and `calibration_status: PUBLIC_CALIBRATION_GAP`.
The existing string `warnings` list additionally contains these exact markers:

```text
provider=synthetic_acceptance
mode=test/acceptance
SYNTHETIC_ONLY_NO_REAL_MODEL_VALIDATION
```

`PUBLIC_CALIBRATION_GAP` and `REAL_MODEL_SMOKE_SKIPPED` remain. These are warning
values, not new response fields. Default and real-provider diagnostics do not
acquire the synthetic markers. No endpoint, schema, error code or snapshot
format changes.

## Version 1 fixture alphabet

Inputs are lossless PNGs decoding to exactly 96 by 96 RGB pixels, uint8, every
channel of every pixel equal to the value below. Use no color profiles or lossy
JPEG conversion. Names are consumer labels; the provider uses decoded pixels.
The Docker acceptance runner also exercises a fixed uniform JPEG whose decoded
pixels match the alphabet exactly; arbitrary lossy conversion is not guaranteed.

| Label | Pixel value | Faces | Anonymous vector | Suggested acceptance decision |
| --- | --- | --- | --- | --- |
| A | 32 | 1 | (1, 0, 0) | NEW_PERSON, person_key `synthetic-a` |
| B | 64 | 1 | (1, 0, 0) | Same NEW_PERSON key and display_name as A |
| C | 160 | 1 | (0, 1, 0) | OUTSIDER (different synthetic identity) |
| D | 224 | 0 | none | No candidate or decision |
| E | 192 | 1 | (0, 0, 1) | IGNORE_FACE |

Each nonempty detection is bbox `[2,2,30,30]`, landmarks
`[[8,10],[25,10],[16,17],[10,25],[23,25]]`, detector score `0.9`.
These are artificial geometry and vectors, never identity confidence.
Other image dimensions, pixel values or nonuniform images produce the existing
DETECTION_INVALID error in a FAILED job, not synthetic guesses for real photos.

Generate input bytes using only the Python standard library:

```python
import struct
import zlib

def fixture_png(value):
    def chunk(kind, body):
        return (struct.pack('>I', len(body)) + kind + body
                + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff))
    pixels = (b'\x00' + bytes([value, value, value]) * 96) * 96
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 96, 96, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))
```

For each fixture, place those bytes in the returned workspace `source_directory`
relative to the operator root. Register a separate source_ref, the actual byte
length/SHA-256, image/png and the current expected_revision. File placement is
the existing out-of-band local source mechanism, not reading service state.

## Expected independent HTTP lifecycle

1. Create a fresh workspace and confirm empty people/assignments. Register A–E.
2. Submit all returned asset_ids with the latest workspace basis and explicit
   policy `{match_similarity: 0.8, match_margin: 0.1, cluster_similarity: 0.8}`.
   These values are synthetic test inputs only, not real-model thresholds.
3. Poll to SUCCEEDED, use result_refs.run_id to create a review. Four candidates
   must be UNKNOWN with null person IDs, no reference scores and
   NON_AUTHORITATIVE authority. There are three provisional groups: A+B, C, E.
   D has no face. Associate candidates with returned asset_ids, never sorted row
   position or a reconstructed ID.
4. Use each candidate's exact expected_state. Submit A+B as NEW_PERSON with
   display_name `Synthetic A`, shared person_key `synthetic-a` and optional
   `enroll_reference: true`; submit C OUTSIDER and E IGNORE_FACE without target
   person fields. Use the review revision. Retry the identical request/key:
   receipt and saved revision stay identical. A new request using the stale
   review revision returns STALE_REVISION. Saving alone creates no person fact.
5. Apply the saved revision. Expect SUCCESS and one new_persons mapping. Read
   one active person, four faces (two ASSIGNED, one OUTSIDER, one IGNORED), two
   assignments. Same-key and different-key duplicate apply both return the
   original receipt with IDEMPOTENT_REPLAY, without changing workspace basis.
6. Publish at the current basis and read/export the snapshot. Check persons,
   faces and assignments against those HTTP projections. ignored_state has one
   ignored=true row for E; provenance has five registered assets and inference
   records (D reports NO_FACES), the applied human events, and the publication
   basis. Each inference manifest is kind synthetic-fixture, embedding_model
   synthetic-acceptance-vectors-v1 and calibration_status SYNTHETIC_ONLY.
   The snapshot compatibility calibration status remains PUBLIC CALIBRATION GAP.
   Receipt source_digest equals the basis; initial snapshot_version is 1;
   payload_sha256 must match raw exported bytes. Exact publish replay and export
   replay never add a publication or human event.
7. Optional existing-person trial: register another A under a new logical
   source_ref after A/B were enrolled. Run it, review its UNKNOWN candidate,
   and expect candidate_person_id equal to the returned human person ID. The
   comparison remains SINGLE_REFERENCE_PERSON_NO_MARGIN_EVIDENCE. Save/apply
   EXISTING_PERSON explicitly; a candidate alone does not assign the face.
8. Restart the service. Old job IDs return JOB_NOT_FOUND even if the same
   request key is reused for a new job. Durable human facts and snapshots remain.

Normal errors, idempotency, revision and restart rules remain those of the
[frozen HTTP contract](public-v0-api-contract.md). No SQLite, private files or
internal objects are necessary to validate the above expectations.

## Determinism and limits

There is no seed, clock, host state or random choice in the provider. Its model
manifest uses SHA-256 of the UTF-8 strings `synthetic-acceptance-detector/v1` and
`synthetic-acceptance-embedding/v1` as synthetic algorithm identifiers, not hashes
of model weights. Its preprocessing identifier is synthetic-acceptance-pixels-v1.
All runs use the existing decoder, ingest, normalization, matching, review,
apply and snapshot code. Real LocalModels and core policy semantics are unchanged.

Identical pixels and policy yield identical detections/vectors and grouping
membership. Repeating matching at the same workspace basis yields the same run
and candidates. Across fresh workspaces, compare fixture-keyed geometry,
grouping, human state, names and assignments: opaque workspace/resource/event
IDs and server-generated timestamps intentionally retain their frozen semantics.
Full snapshot bytes are identical for replay of one publication, not across
independent workspaces or new publication versions. No fabricated fixed clock
or replacement identity scheme is introduced to claim byte-level determinism.

Use separate workspaces for synthetic and real models. Existing core model-space
pinning rejects mixing inference spaces. This acceptance support grants no
production readiness, model redistribution approval or real-model calibration.
