# ClapClip Worker: Local AI Media Processing

ClapClip Worker is an open source Python application for **photo face swap, background removal, image enhancement, and video and audio downloads**. Use its local web interface on your own computer, or run distributed worker nodes connected to a private task API.

The local workbench lets you upload images, choose a model, preview results, and save the output in your browser. Distributed mode uses the same processing engines to handle jobs from your own backend.

[Local setup](#local-web-interface) · [Distributed deployment](#private-distributed-workers) · [Model licenses](THIRD_PARTY.md) · [Contributing](CONTRIBUTING.md)

## What it does

| Tool | Input and output | Details |
| --- | --- | --- |
| **AI photo face swap** | Target photo + source photo → swapped photo | Local face detection, alignment, and blending. Supports Baseline and the optional Robust, Balanced, and Accurate models. |
| **Image background removal** | Photo → transparent PNG | Uses an ISNet ONNX model to separate the subject from its background. |
| **Image enhancement** | JPEG, PNG, or WebP → PNG | Models cover upscaling, deblurring, artwork, and portrait restoration. Available options depend on installed weights. |
| **Video and audio downloader** | Supported public media link → MP4, WebM, or MP3 | Uses yt-dlp and FFmpeg. Video fits within 1280 × 720; video and audio output start at the beginning and last up to 300 seconds. |

Download processing can scale larger sources and trim longer recordings. There is no fixed application-level media file size cap; network protections and timeouts still apply. Downloads do not use browser cookies or support private, DRM-protected, or live media.

**Model weights are installed separately.** The source code is MIT-licensed; this does not grant rights to third-party models or media. Read [Third-party software and models](THIRD_PARTY.md) before choosing weights, especially for commercial use.

## Choose a deployment mode

| | Local web interface | Private distributed worker |
| --- | --- | --- |
| Use it for | Processing images on your computer | Providing compute for your own application |
| Entry point | `python -m mediaworker local` | `python -m mediaworker run --all-capabilities` |
| Interface | Browser at `http://127.0.0.1:8765` | Your backend's task API |
| Account | No account required | Node authentication provided by your backend |
| Image processing | Runs on the local machine | Runs on the worker machine |
| Backend required | No | Yes; the server is not included |

## Requirements and platform support

Use **Python 3.13 or later**. Downloads also need **FFmpeg, FFprobe, and Node.js 22+** on `PATH`. Install the ONNX Runtime provider appropriate for your hardware.

| Platform | Runtime notes |
| --- | --- |
| Windows | A pinned DirectML environment is provided. CUDA can be installed in a separate environment. |
| Linux | Install a compatible ONNX Runtime provider. Image enhancement requires CUDA. |
| macOS | The workbench can run supported tools, but the current image enhancement engine requires DirectML or CUDA and is unavailable on macOS. |

A GPU is required for the current enhancement engine. Other tools depend on their installed models and runtime; starting the web interface does not mean every tool is ready.

## Local web interface

Clone the source:

```bash
git clone https://github.com/ClapClip/clapclip-worker.git
cd clapclip-worker
```

On **Windows with DirectML**, install in this order:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-unified-windows.txt
.venv\Scripts\python -m pip install --no-deps -e . "insightface==2.0"
```

This avoids replacing DirectML with InsightFace's CPU ONNX Runtime dependency. Keep CPU, DirectML, and CUDA runtime installations in separate environments.

On **macOS or Linux**:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[downloader]'
```

Install models for the tools you want to use, then start the workbench:

```powershell
# Windows
.\scripts\start-local.ps1
```

```bash
# macOS / Linux
bash scripts/start-local.sh
```

Open **http://127.0.0.1:8765**. Upload images or analyse a supported public media link, choose an available model or format, and save the result. Missing models leave the affected tool unavailable without preventing the workbench from starting.

You can also run `python -m mediaworker local` inside the configured environment. Local mode does not load the default `.env`, register a node, or claim remote jobs. To configure model paths, copy `.env.local.example` to `.env.local` and pass `--env-file .env.local` explicitly.

The server binds to loopback only. Once dependencies and weights are installed, image inference runs on the host; video downloads require internet access. Do not expose the local workbench through a public reverse proxy.

### Install models

Default model location: `~/.mediaworker/models/`.

```text
models/
  arcface_w600k_r50.onnx
  inswapper_128_fp16.onnx        # Baseline; inswapper_128.onnx also supported
  hyperswap_1a_256.onnx          # Robust, optional
  hyperswap_1b_256.onnx          # Balanced, optional
  hyperswap_1c_256.onnx          # Accurate, optional
  cutout/isnet-general-use.onnx
  enhance/yoloface_8n.onnx       # Also needed for local full-photo face swap
```

Local face swap needs the identity model, a swap model, and the detector. Enhancement needs additional model files. The local workbench does not automatically fetch missing weights. See the [local model setup](docs/local-mode.md) and [enhancement model table](docs/distributed-mode.md) for paths and configuration.

## Private distributed workers

Deploy a backend that implements the matching authentication, task queue, lease, and storage API. This repository provides the compute node, not a complete hosted service or a general-purpose queue server.

After installing the runtime and models, copy `.env.example` to `.env` and configure your API URL, node identity, authentication, and capabilities. Keep the real configuration private.

```bash
python -m mediaworker run --all-capabilities
```

One instance can provide all four tools. Three image pollers and one download poller share **at most three execution slots**, with **one download at a time**. Model locks and hardware can further limit simultaneous inference. Enable only capabilities supported by that machine.

Nodes initiate outbound connections and use presigned URLs to retrieve inputs and upload results. They do not need database credentials, storage access keys, or an inbound listening port. Leases, heartbeats, and stale-attempt checks prevent expired attempts from publishing results.

Local face swap accepts full photos and performs detection and blending itself. The distributed face-swap protocol expects aligned face crops from your frontend. See [distributed deployment](docs/distributed-mode.md) for exact inputs, authentication, models, and optional background supervision.

## Documentation and development

- [Local deployment and model paths](docs/local-mode.md)
- [Distributed nodes, GPU setup, and authentication](docs/distributed-mode.md)
- [Download queue integration](docs/download-queue.md) and [download policy](docs/download-policy.md)
- [Architecture and development](docs/architecture.md)
- [Contribution and release checks](CONTRIBUTING.md)
- [Security reporting](SECURITY.md)

The detailed deployment and architecture guides are currently in Chinese. This README includes English installation commands and the main operating requirements. The Python package and CLI retain the name `mediaworker`.

Run offline tests from the configured environment:

```bash
# macOS / Linux
MEDIAWORKER_LOAD_DOTENV=0 python -m unittest discover -s tests -v
```

```powershell
# Windows PowerShell
$env:MEDIAWORKER_LOAD_DOTENV = "0"
python -m unittest discover -s tests -v
```

Tests use generated media, temporary directories, and mock APIs. They do not establish model quality, GPU performance, or successful downloads from a live platform. Source lives in `mediaworker/`, tests in `tests/`, and deployment helpers in `scripts/`.

## License

Source code is available under the [MIT license](LICENSE). Models, dependencies, and external tools retain their own licenses. Do not commit credentials, weights, logs, or user media. See [THIRD_PARTY.md](THIRD_PARTY.md) for the publication boundary.
