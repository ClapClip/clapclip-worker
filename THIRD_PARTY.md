# Third-party software and models

The MIT license in this repository covers ClapClip Worker source code. It does
not relicense dependencies, model weights, training data, input photographs,
downloaded media or third-party executables. No weights, FFmpeg/Node binaries,
user media or production credentials are included in our source distribution.

## Python dependencies and external tools

Dependencies are installed separately and retain their own notices. The declared
packages are in `pyproject.toml`; the tested Windows DirectML pins are in
`requirements-unified-windows.txt`. Do not treat this project's MIT license as
permission to redistribute a complete Python environment.

- [InsightFace](https://github.com/deepinsight/insightface): SDK source is MIT;
  upstream pretrained models have separate, generally non-commercial research
  terms. See its [model notice](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md).
- [ONNX Runtime](https://github.com/microsoft/onnxruntime/blob/main/LICENSE):
  inspect its license and third-party notices when redistributing runtime binaries.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE): its source license
  does not grant rights to media, bundled components or supported websites.
- [FFmpeg](https://ffmpeg.org/legal.html): licensing depends on the build and
  enabled components. Users install a suitable build separately.
- [Node.js](https://github.com/nodejs/node/blob/main/LICENSE): installed separately.

## Model files

Filenames describe the expected inference interface, not proof of origin or
permission. An ONNX conversion does not change the original model's license.
Keep the provider's license, provenance and permission with each local weight.

| Files / family | Upstream reference and publication boundary |
| --- | --- |
| ArcFace / `arcface_w600k_r50.onnx`, InSwapper | [InsightFace](https://github.com/deepinsight/insightface). Its code license does not grant commercial rights to pretrained weights. Obtain the required separate permission. |
| `hyperswap_1a/b/c_256.onnx` | Supplied by the operator. This repository does not establish their original licensing or redistribute them. Verify permission with the actual provider before use or sharing. |
| `isnet-general-use.onnx` | Downloaded from the configured [rembg release](https://github.com/danielgatis/rembg/releases/tag/v0.0.0); see [DIS](https://github.com/xuebinqin/DIS). Verify the particular weights' terms, not just a wrapper's source license. |
| Real-ESRGAN | [Upstream](https://github.com/xinntao/Real-ESRGAN/blob/master/LICENSE). The exact converted weights and their provenance must be checked separately. |
| SPAN, NAFNet, Real-CUGAN | Operator-provisioned ONNX exports. Retain the actual model provider's notices and conversion provenance; this project makes no blanket commercial-use grant. |
| GFPGAN / GPEN | [GFPGAN](https://github.com/TencentARC/GFPGAN/blob/master/LICENSE), [GPEN](https://github.com/yangxy/GPEN). Check the exact code, weights and any incorporated components. |
| `yoloface_8n.onnx` | Operator-provisioned detector. Verify the actual exporter/provider's license. Do not assume every YOLO-derived detector is permissively licensed. |

The optional private model repository is configured by each deployment. Source
releases contain model names, integrity hashes and loader code, not the model
assets or a commercial model license. We do not publish a model download bundle.

Only contribute code and assets you have the right to license. Preserve any
upstream copyright and attribution if third-party source is incorporated in a
future change; do not copy it into this repository under a new MIT heading.
