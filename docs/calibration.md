# Public V0 calibration and model boundary

**PUBLIC CALIBRATION GAP** remains open. Synthetic acceptance proves core behavior, not recognition accuracy or production defaults. No private calibrated thresholds, formal photos, historical rosters or fitting outputs were used. No model weights were downloaded or redistributed.

| Behavior | V0 contract | Evidence / limitation |
|---|---|---|
| `confidence` | Required explicit YuNet confidence input | No production value chosen; validated finite range only |
| `nms` | Required explicit YuNet NMS input | No production value chosen |
| `top_k` | Required explicit YuNet detection candidate cap | Positive bounded integer; no calibrated default |
| `match_similarity` | Required explicit cosine candidate cutoff | No automatic human identity mutation, regardless of score |
| `match_margin` | Required explicit best/second-best candidate margin | Applied when there are at least two eligible reference persons; one-person case explicitly says `SINGLE_REFERENCE_PERSON_NO_MARGIN_EVIDENCE`, applies only the absolute candidate cutoff, and grants no identity |
| `cluster_similarity` | Required explicit complete-link threshold | Every cross-component pair must satisfy it; same-asset and different detections from same content cannot link |
| No references | Unknown provisional groups | Never interpreted as OUTSIDER; no person ID minted by inference |
| Human terminal state | No automatic new grouping/assignment | ASSIGNED, OUTSIDER, IGNORED and UNASSIGNED are preserved; explicit human correction required |
| Reference enrollment | Explicit human `enroll_reference` | Same compute/model space, still-active human assignment; duplicate content detection references do not create independent votes |
| Resource limits | Existing decoder byte/pixel limits; 256 faces per asset, 512 faces per matching call | Engineering safety bounds, not recognition thresholds; over-limit input fails explicitly |

`LocalModels` uses the existing YuNet + AuraFace adapters. Detector confidence/NMS/top-k are all passed explicitly. The adapter verifies caller-supplied hashes and records a path-free model manifest. The current AuraFace preprocessing is still the existing convention-based implementation, labeled `auraface-convention-unvalidated-v1`. Model-space identity includes model hashes, dimension, detector settings and preprocessing version; changing it in an occupied V0 workspace is explicitly refused. Model migrations and SFace integration are deferred.

Existing SFace code can be public generic code without being a validated V0 provider. V0 does not silently swap models or compare their embeddings. Existing constructor defaults elsewhere are not automatically accepted as V0 calibration.

The only numeric similarity thresholds in new test fixtures are deliberately **SYNTHETIC_ONLY** values in `scripts/public_v0_fixture.py`. These fixtures use generated solid-color images and prescribed vectors. Runtime modules never import the fixture. Real image decoding is exercised for JPEG and PNG; detection/embedding calls are counted, but synthetic vectors are not real model outputs.

This source repository contains no model weights. Optional external-model smoke is separate from synthetic validation and must use explicitly supplied paths and anonymous input. Even a future synthetic-image real-model smoke would only prove load/shape/basic execution, not useful accuracy on actual faces.

Remaining gates before claiming a validated real-photo product: model source/usage terms and hash provenance; preprocessing/alignment/orientation validation; consented public evaluation data; public matching/clustering calibration and quality measurements. Source is Apache-2.0; model weights retain separate terms. Synthetic validation does not establish model provenance or redistribution permission.
