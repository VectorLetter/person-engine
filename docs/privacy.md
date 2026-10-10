# Privacy and biometric data

Face embeddings and identity facts are sensitive data. This local-first design
is not anonymization, a consent mechanism or a legal compliance guarantee.
Operators must assess their own permissions, use case, retention and applicable
obligations before processing personal photos.

Original photos are opened read-only; imports copy their bytes into an engine-owned
workspace. The workspace can contain photo copies, face geometry, embeddings,
logical asset references, person names, human review decisions, audit events and
immutable snapshots. Protect the entire workspace, its exports and backups with
appropriate filesystem access controls. The service has no user authentication;
only trusted local callers should be able to reach it. One process owns a workspace.

Snapshots omit pixels, crops, model/photo filesystem paths and embedding vectors,
but may contain identity metadata: user-supplied display names, source references,
face/asset/person IDs, assignments and human provenance. Exporting a snapshot does
not anonymize it. Review its content and recipients before sharing it.

Do not upload workspace databases, registry files, embeddings, real snapshots,
photos or secrets to public issues. Use the generated synthetic fixtures for bug
reports, and follow [security reporting](../SECURITY.md). Tests generate artificial
pixels and vectors in disposable directories; these are not real-person datasets.

Deleting this source checkout, a virtual environment or a Docker container does
not delete external runtime workspaces, imported photo copies, exports or backups.
Account for those separately in your retention/deletion process. There is no
complete data-erasure or backup-retention service in Public V0.

The engine has no automatic model download, telemetry or cloud upload mechanism.
Installation and image builds obtain dependencies over the network; the default
Swagger `/docs` UI uses an external CDN. The JSON API itself does not require that
CDN. Local processing does not protect against a compromised host or a hostile
local process with access to workspace files. Python test audit guards are useful
verification tools, not an operating-system sandbox for native libraries.
