# External model policy

No weights are included, copied, downloaded or redistributed. `LocalModels` accepts explicit external YuNet/AuraFace paths, expected SHA-256 hashes, confidence, NMS and top-k. The workspace pins its compute space. SFace generic code is retained but is not a V0 ingestion fallback.

Verify a user-supplied external model against its separately verified provenance:

```sh
python scripts/verify_model.py --path external-model.onnx --sha256 EXPECTED_LOWERCASE_SHA256
```

The file path is an explicit caller argument, not a baked-in search path. The tool does not copy files or establish license clearance. No expected hash is inherited from a local model collection.

| Model | Official declaration | Repository policy |
|---|---|---|
| YuNet | MIT in the official model directory | EXTERNAL_ONLY; preserve official copyright and license if later redistributed |
| SFace | Apache-2.0 in the official model directory | EXTERNAL_ONLY; not the integrated V0 provider |
| AuraFace-v1 | Official card declares Apache-2.0 | **EXTERNAL_ONLY / USER_SUPPLIED / REDISTRIBUTION_NOT_VERIFIED**; the declaration does not close a specific weight file's provenance |

See [licensing](../docs/licensing.md). Optional external-model smoke must use anonymous/synthetic input. It proves at most load/basic execution, not recognition quality; it cannot close **PUBLIC CALIBRATION GAP** or authorize redistribution.

Unresolved AuraFace weight provenance does not block code publication, but does not
establish permission to redistribute weights. Project Apache-2.0 does not relicense
external models. No automatic downloader is supplied.
