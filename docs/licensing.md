# Licensing boundaries

Project source code and documentation: **Apache License 2.0**, copyright
**2026 VectorLetter**. See [LICENSE](../LICENSE). This grant does not relicense
Python dependencies, operating-system components or third-party model weights.

## External models

| Model | Official declaration | Repository policy |
| --- | --- | --- |
| YuNet | [Official model license](https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/LICENSE): MIT | EXTERNAL_ONLY / USER_SUPPLIED; retain applicable copyright/license notices if redistributed |
| SFace | [Official model license](https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_recognition_sface/LICENSE): Apache-2.0 | EXTERNAL_ONLY / USER_SUPPLIED; generic adapter, not the integrated Public V0 provider |
| AuraFace-v1 | [Official model card](https://huggingface.co/fal/AuraFace-v1/blob/main/README.md) claims Apache-2.0 | EXTERNAL_ONLY / USER_SUPPLIED / **REDISTRIBUTION_NOT_VERIFIED** |

Mutable upstream declarations do not establish a particular weight file's immutable
revision, provenance, training-data permissions or complete redistribution notices.
The operator must supply model files and expected hashes. Hash checks establish
integrity against a caller's value, not permission. No weights, automatic downloads
or unverified direct downloaders are included.

AuraFace's unresolved weight provenance does not block publication of this source
code; it does not mean weight redistribution has been verified or approved.
Model license declarations do not close the real-model calibration/quality gap.

## Dependencies and binary distributions

Python packages retain their own licenses. See [third-party notices](../THIRD_PARTY_NOTICES.md)
and the [core/build](dependency-licenses.json) and [API/test](api-dependency-licenses.json)
metadata inventories. Package metadata is a discovery aid, not a replacement for
full wheel licenses, bundled component notices or an assessment of a binary image.
No third-party packages or binaries are vendored in this source repository.

A future prebuilt image distribution must review and supply applicable notices for
Python wheels, their bundled libraries, the base image and operating-system
packages. Building locally is not a claim that this source notice inventory is a
complete binary redistribution bundle.

## Frozen diagnostics compatibility

The existing `/v1` diagnostic warning `LICENSE_UNDECIDED` is retained as a legacy
wire-compatible token in Public V0 0.1.0. It does not describe the current source
license: the authoritative source grant is Apache-2.0 in LICENSE. The warning's
future correction requires a separately reviewed runtime change; this publication
cleanup does not change any API response, schema or runtime behavior.
