# Public V0 architecture

```text
fresh workspace → source-ref registration → decode/detect/embed
→ non-authoritative match/groups → human review → atomic apply
→ immutable person-fact snapshot → independent JSON consumer
```

`person_engine.public_v0` is the library boundary. The top-level module and `person-engine` console script forward to its local JSON CLI. Registration and all human facts share a generated SQLite schema, owned only by the engine. Source references identify logical assets; equal bytes may share storage without merging asset identity. Detection content identity and asset-scoped face identity remain distinct.

Writer operations use a kernel lease and SQLite transactions. Review freezes source facts and candidate IDs, then uses revision compare-and-swap. Human events, projections, reference changes and apply receipts commit together. SQLite recovers interrupted local transactions under that same lease. No external backup dispatcher or compatibility migration is required.

Unknown groups are provisional. Only an explicit human decision creates a person, assigns a face or enrolls a reference. Model spaces cannot mix. Negative human decisions and corrections invalidate affected references. OUTSIDER, IGNORED, UNKNOWN and UNASSIGNED are distinct.

Snapshots commit complete canonical JSON plus receipt in SQLite. Export is a separate atomic, no-overwrite file operation, so a failed export can be retried without recreating facts. Consumers require only a JSON reader; their acknowledgement does not confer engine authority.

The runtime import closure includes the audited generic dependencies used by these entry points. Generic registry helpers reachable through the decoder's existing import chain retain their stable protocol namespace; a protocol label is not a dependency on another application. No application consumer source, database, policy, private operator, historical data or environment-specific configuration is included.

Schemas are inline and created only for a new workspace. Existing database migration is intentionally unsupported. The dependency graph is regenerated with `scripts/build_dependency_graph.py`; all non-standard Python imports must map to declared package dependencies.
