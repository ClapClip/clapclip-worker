# ClapClip Worker: Local AI Media Processing

ClapClip Worker is an open source Python application for **photo face swap, background removal, image enhancement, and video and audio downloads**. Use its local web interface on your own computer, or run distributed worker nodes connected to the central Web-ClapClip backend at `https://www.clapclip.net`.

The local workbench lets you upload images, choose a model, preview results, and save the output in your browser. Distributed mode uses the same processing engines for website queues managed by Web-ClapClip; Workers do not connect to individual business-site APIs.

Distributed face swap also supports bounded animated GIF targets with explicit
queue gating, identity matching and GIF output. See the [GIF contract](docs/faceswap-gif.md).

[Cross-platform quick start](docs/portable-setup.md) · [Local setup](#local-web-interface) · [Distributed deployment](#private-distributed-workers) · [Model licenses](THIRD_PARTY.md)

An [unattended managed-installation candidate](docs/one-click-contract.md) adds
no-argument dependency setup, account/password-only enrollment, real boot startup,
one Worker per machine and drained official-main updates. Its Web-ClapClip backend
V1 contract passed local acceptance; deployed APIs, models and real-machine
installation remain unverified, so it is not an online-ready installer. The
existing local/manual installer below remains available.

## ClapClip websites

Explore the ClapClip desktop app and online media tools:

| Website | Product |
| --- | --- |
| [clapclip.net](https://www.clapclip.net/) | ClapClip for Windows: local photo and video face swap, image enhancement, and other media tools. |
| [faceswap.free](https://faceswap.free/) | Online photo face swap using a target photo and a source face. |
| [cutout.free](https://cutout.free/) | Online image background removal and transparent PNG export. |
| [enhance.place](https://enhance.place/) | Online AI image enhancement and photo upscaling. |
| [clapclip.art](https://clapclip.art/) | Online video and audio downloads from supported public media links. |

These are separate hosted products. Their processing, access, and retention policies are independent of the local workbench in this repository.

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
| Interface | Browser at `http://127.0.0.1:8765` | Central Web-ClapClip task API |
| Account | No account required | Account and node authorized by Web-ClapClip |
| Image processing | Runs on the local machine | Runs on the worker machine |
| Backend required | No | Yes; the server is not included |

## Requirements and platform support

The portable installer uses **ordinary native CPython 3.13** with pinned wheel dependencies: Windows x64, macOS 13+ **Apple Silicon M-series only (native arm64)**, and glibc Linux 2.27+ x64/ARM64. Intel Macs and Rosetta Python are not supported. Other architectures, free-threaded Python and newer Python minors are checked explicitly rather than silently built from source. Downloads also need **FFmpeg, FFprobe, and Node.js 22+** on `PATH`.

| Platform | Runtime notes |
| --- | --- |
| Windows | A pinned DirectML environment is provided. CUDA can be installed in a separate environment. |
| Linux | Install a compatible ONNX Runtime provider. Image enhancement requires CUDA. |
| macOS / M-series | Use arm64 Python, or universal2 Python running natively as arm64. Intel/Rosetta is unsupported. Enhancement supports CoreML/CPU; licensed enhancement weights must be installed separately. |

Enhancement requires DirectML/CUDA on Windows/Linux; native M-series macOS also supports CoreML/CPU. Other tools depend on their installed models and runtime; starting the web interface does not mean every tool is ready.

## Local web interface

Clone the source:

```bash
git clone https://github.com/ClapClip/clapclip-worker.git
cd clapclip-worker
```

Use the same installer, doctor and foreground launcher on all supported systems:

```powershell
# Windows
.\scripts\setup-worker.ps1 install
.\scripts\setup-worker.ps1 doctor --load-models
.\scripts\setup-worker.ps1 start
```

```bash
# macOS / Linux
bash scripts/setup-worker.sh install
bash scripts/setup-worker.sh doctor --load-models
bash scripts/setup-worker.sh start
```

The installer preserves existing environments/configuration and creates `.venv-portable` plus private example configurations with independent node/state identities. It does not download models, authenticate, claim jobs, start the worker or install autostart services. Prepare licensed models before `start`; missing capabilities are reported separately from a successful dependency installation. See [the support matrix, system-tool installation and troubleshooting](docs/portable-setup.md).

The portable Windows/Linux environment is a CPU baseline; macOS may also expose CoreML. Enhancement uses CoreML/CPU on native M-series macOS and requires DirectML/CUDA on Windows/Linux. For an independent Windows GPU environment use the [existing DirectML setup](docs/local-mode.md#本地安装); never mix CPU, DirectML and CUDA distributions in one environment.

Open **http://127.0.0.1:8765**. Upload images or analyse a supported public media link, choose an available model or format, and save the result. Missing models leave the affected tool unavailable without preventing the workbench from starting.

You can also run `python -m mediaworker local` inside a manually configured environment. Local mode does not load the default `.env`, register a node, or claim remote jobs. For the portable launcher edit `.env.portable.local`, or pass an explicit `--env-file` for your model paths.

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

Distributed Workers use only `https://www.clapclip.net` for authentication, task queues, leases and storage authorization. This repository provides the compute node. The managed installer's central V1 contract passed local acceptance; local tests do not establish deployed APIs or online readiness.

After portable installation, keep `API_BASE_URL=https://www.clapclip.net` in `.env.portable` and fill the authorized permanent account credentials, matching Cognito configuration and allowed capabilities. Each machine/account needs a unique stable node ID and separate runtime/token paths. The offline doctor does not authenticate; the explicit foreground start command does:

```bash
python3.13 scripts/setup-worker.py doctor --mode distributed --env-file .env.portable --load-models
python3.13 scripts/setup-worker.py start --mode distributed --env-file .env.portable
```

Windows can use `setup-worker.ps1` with the same arguments. The portable launcher enables loaded capabilities intersected with server authorization; it does not let an unavailable enhancement engine block other usable tools. Manually managed GPU deployments can retain the strict `python -m mediaworker run --all-capabilities` entry. Three image pollers and one download poller share **at most three execution slots**, with **one download at a time**. Model locks and hardware can further limit simultaneous inference.

Nodes initiate outbound connections and use presigned URLs to retrieve inputs and upload results. They do not need database credentials, storage access keys, or an inbound listening port. Leases, heartbeats, and stale-attempt checks prevent expired attempts from publishing results.

Local face swap accepts full photos and performs detection and blending itself. The distributed face-swap protocol expects aligned face crops from your frontend. See [distributed deployment](docs/distributed-mode.md) for exact inputs, authentication, models, and optional background supervision.

## Documentation and development

- [Cross-platform installation, doctor and manual start](docs/portable-setup.md)
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
