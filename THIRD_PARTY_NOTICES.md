# Third-party notices and distribution scope

Person Engine source: Copyright 2026 VectorLetter, Apache-2.0; see [LICENSE](LICENSE).
This repository distributes project source and fixture generators, not dependency
wheels, model weights or a prebuilt container image. Third-party software retains
its own copyrights and licenses; the project grant does not supersede them.

## Python dependencies

The pinned requirements files identify exact distributions/versions. The
[core/build inventory](docs/dependency-licenses.json) and [API/test inventory](docs/api-dependency-licenses.json)
record upstream package metadata. Consult each installed distribution's full
license files and third-party notices before redistributing its binaries.

| Component | License summary and binary notice boundary |
| --- | --- |
| NumPy | BSD-3-Clause; wheels may include OpenBLAS and compiler runtime notices |
| opencv-python / OpenCV | Python packaging MIT; OpenCV Apache-2.0; bundled codecs/libraries have separate terms, including FFmpeg LGPL and, in relevant non-headless Linux wheels, Qt LGPL |
| ONNX Runtime | MIT; preserve its third-party notice material |
| FastAPI, Pydantic / pydantic-core | MIT |
| Starlette, Uvicorn | BSD-3-Clause |
| Other Python dependencies and build/test tools | Retain their own license files; inventories include MIT, BSD, Apache-2.0, PSF and MPL-2.0 terms; build-tool vendored components may have additional terms |

This is a source-distribution inventory, not a complete binary notice bundle.
Before distributing a prebuilt image, collect exact wheel/base/OS component
licenses and required notices, review applicable bundled-library obligations,
and publish the resulting notices with that image. This follow-up does not block
publication of this source-only candidate.

## Model weights

No model weights are distributed. Users supply files themselves and must verify
provenance, terms and expected hashes independently.

- YuNet: [official model directory license](https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/LICENSE) is MIT.
- SFace: [official model directory license](https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_recognition_sface/LICENSE) is Apache-2.0; the generic adapter is not the integrated Public V0 provider.
- AuraFace-v1: the [model card](https://huggingface.co/fal/AuraFace-v1/blob/main/README.md)
  claims Apache-2.0. Status remains **EXTERNAL_ONLY / USER_SUPPLIED / REDISTRIBUTION_NOT_VERIFIED**.
  The declaration alone does not resolve a specific file's provenance or redistribution permission.

Source publication does not constitute clearance to distribute AuraFace or any
other model weights. See [model policy](models/README.md) and [licensing](docs/licensing.md).
