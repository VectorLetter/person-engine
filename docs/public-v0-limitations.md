# Public V0 limitations

**Experimental Developer Preview — Public V0**, package version 0.1.0.

- Python library/CLI, local FastAPI `/v1`, Docker/Compose and explicit local import
  helper are available. There is no HTTP upload or automatic photo discovery.
- JPEG/PNG decoding is portable. Public V0 registration/import accepts JPEG/PNG
  only. Linux HEIC is unsupported; a shared native macOS HEIC decoder does not
  extend this public media contract.
- Linux arm64 Docker acceptance is verified. Linux amd64 is **METADATA_ONLY**;
  a multi-architecture base index is not proof of amd64 runtime success.
- Real-model accuracy, AuraFace preprocessing and public matching/clustering
  calibration are unvalidated. `PUBLIC_CALIBRATION_GAP` stays open. Synthetic
  acceptance is **TEST / ACCEPTANCE ONLY**, not face recognition evidence.
- AuraFace is **EXTERNAL_ONLY / USER_SUPPLIED / REDISTRIBUTION_NOT_VERIFIED**.
  Operators supply and verify model files. No weights or downloader are included.
- No authentication or multi-user isolation. Localhost-only, single-machine,
  single-process service with one writer per dedicated workspace.
- Jobs are in memory. Restart loses job status and request deduplication;
  old job IDs return `JOB_NOT_FOUND`. Durable core facts/receipts/snapshots persist.
  There is no persistent queue, job recovery/list API or distributed scheduler.
- Matching is bounded to 512 faces; at most 1024 jobs exist per service lifetime.
  Multi-asset ingestion is not one all-or-nothing transaction.
- Existing workspace/schema migration and switching model spaces in an occupied
  workspace are unsupported. Use a new dedicated workspace.
- Source references, display names and snapshots can identify people. Local-first
  is not anonymization. See [privacy](privacy.md).
- Source uses Apache-2.0. Dependencies and model weights keep separate terms;
  see [licensing](licensing.md). Prebuilt-image redistribution needs its own
  component notice review. No production-readiness or legal compliance claim.
