# Snapshot contract — person-facts/v0.1

A published snapshot is a complete immutable document, scoped by `workspace_id`. `snapshot_id` and `snapshot_version` identify its publication; versions increase within a workspace. `schema_version` is distinct from the internal SQLite schema. `producer_version` identifies the public producer contract.

| Field | Meaning |
|---|---|
| workspace_id | Persistent opaque workspace namespace |
| snapshot_id / snapshot_version | Immutable publication ID and increasing version |
| schema_version / producer_version | Explicit consumer/producer contract |
| persons | Stable IDs, display names and active/retired state |
| faces | Asset-scoped face IDs, detection binding, geometry, model space and human state |
| assignments | Current human face-to-person facts and event references |
| ignored_state | Explicit reviewed states; ignored and assigned are mutually exclusive |
| provenance | Source-ref registration, inference manifest, human events/rejections and frozen source basis |
| source_digest | Canonical domain content digest at the publication basis |
| generated_at | Timestamp fixed on first publication and preserved on replay |
| compatibility | Full-snapshot mode, minimum reader schema, source revision and human-only authority |

UNKNOWN means not human-reviewed, OUTSIDER means an actual face outside the roster, IGNORED means a human false-detection decision, and UNASSIGNED means an explicit human removal of assignment. Candidate scores/groups are not published as human assignments.

The publication receipt separately records `content_digest` (canonical JSON document hash) and `payload_sha256` (actual UTF-8 canonical JSON plus newline bytes). Excluding these from the document itself avoids circular hashes. Exact request replay returns the original receipt, timestamp and bytes; changed semantics under one request key conflict. A new request may publish a new version without changing an earlier one.

`publish_snapshot` commits the entire JSON and receipt atomically in SQLite after source-basis checks. `export_snapshot` writes the same bytes through a staged, flushed, atomic no-overwrite file creation. Export failure leaves the published authority intact and can be retried. Existing conflicting output bytes are never replaced. Only complete published documents can be exported.

Consumers may validate the payload hash, schema, referential integrity, workspace/version and source digest using only exported JSON and its receipt. Names/source IDs are user content. No absolute photo/model paths, pixels, crops, embeddings or consumer-specific policy are exported. No historical consumer compatibility is implied.
