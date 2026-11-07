# Windows、macOS、Linux：同一套安装与诊断

首次部署先安装，再诊断，最后手动启动。安装器只管理新的 `.venv-portable`，不会覆盖 `.venv`、DirectML/CUDA 环境、现有 `.env`、模型或令牌，也不创建计划任务、服务、LaunchAgent、自启或自动更新。安装和 doctor 不登录、不领取任务、不下载模型。

## 支持范围

使用**普通原生 CPython 3.13**，补丁版本使用 Python 官方当前受支持的更新。这个安装组合不接收 PyPy、free-threaded Python 或 3.14/3.15。

| 系统与架构 | 安装组合 | 能力边界 |
| --- | --- | --- |
| Windows 10/11 x64 | CPython 3.13 x64、CPU ONNX Runtime | 换脸、抠图、下载；增强使用另外的 DirectML/CUDA 环境 |
| macOS 13+ Apple Silicon M 系列 | arm64 CPython 3.13，或以原生 arm64 运行的 universal2 CPython 3.13 | 换脸、抠图、下载；可用 provider 以 doctor 为准 |
| Linux x64 / ARM64 | CPython 3.13、glibc >=2.27，例如 Ubuntu 22.04+ | CPU 基线；增强需要另行配置并验证 CUDA |
| Intel Mac、Rosetta Python、Windows ARM64/32 位、Alpine/musl、其他架构/旧系统 | 不在支持范围内 | 安装器明确停止，不静默编译或承诺支持 |

CPU provider 能运行的引擎不等于 GPU 加速。macOS 的 CoreML provider 也不等于每个模型都在 GPU/ANE 上完整执行：ONNX Runtime 会按算子支持划分执行。**当前增强引擎只接受 DirectML/CUDA，CPU/CoreML 不满足要求。** 安装器不会改增强算法、放松 provider 门限，或安装 Windows GPU 库到 Mac。

`requirements-portable.txt` 固定 numpy 2.2.6、OpenCV 4.12.0.88、ONNX Runtime 1.23.2、InsightFace 2.0 等主依赖；InsightFace 2.0 提供纯 Python wheel，不需要为普通运行编译旧版 C++ 扩展。pip 只接受依赖 wheel，找不到就停止。现役 Windows 的 `requirements-unified-windows.txt` 独立保留，不会被这个 CPU 组合覆盖。

## 1. 安装系统工具

需要 Python 3.13；下载能力另外需要 **Node.js 22+、FFmpeg 和 FFprobe**，FFmpeg 应包含 `libx264/libvpx-vp9/aac/libopus/libmp3lame`。它们缺失只影响下载能力，不应阻止已就绪的图片引擎。安装器会提供修复入口，不自动执行 sudo、Homebrew、winget 或系统安装包。

- **Mac（仅 M 系列）**：在未启用 Rosetta 的原生终端中，从 [Homebrew](https://brew.sh) 安装包管理器后执行 `brew install python@3.13 ffmpeg node`。Homebrew 当前推荐 macOS 15+；macOS 13/14 用户不要假定有 bottles。也可使用 [Python.org 的 arm64/universal2 安装器](https://www.python.org/downloads/macos/)，确保 Python 实际以 `arm64` 原生运行，搭配 [Node.js 安装器](https://nodejs.org/en/download)和 [FFmpeg 官方下载入口](https://ffmpeg.org/download.html)。Intel Mac 不支持，不提供 Intel 安装路径。
- **Windows**：[Python.org](https://www.python.org/downloads/windows/) 安装普通 3.13 x64，[Node.js](https://nodejs.org/en/download) 安装 22+，从 [FFmpeg 下载页](https://ffmpeg.org/download.html)选择带 FFprobe 和上述编码器的构建。将工具的 `bin` 目录加入 PATH，然后重新打开终端。
- **Linux**：使用发行版支持的 Python 3.13 / venv 安装方式，或 [Python 官方入口](https://www.python.org/downloads/source/)。Ubuntu/Debian 的媒体与 OpenCV 系统库：`sudo apt-get update && sudo apt-get install ffmpeg libgl1 libglib2.0-0`；如果发行版提供 `python3.13-venv` 则一并安装。Node.js 按官方入口安装 22+，不要默认发行版旧 Node 包已经满足要求。

M 系列 Mac 安装 Python.org 版本后，按官方说明运行安装目录中的 `Install Certificates.command`。不要替换系统 Python，也不要通过 Rosetta 混装 x86_64 环境；检测到 Rosetta 时先关闭终端的“使用 Rosetta 打开”，改用原生 arm64 Python 后重新创建独立环境，保留旧配置。

## 2. 一次安装，可安全重跑

在源码根目录执行；路径包含空格也可以。

```powershell
# Windows
.\scripts\setup-worker.ps1 install
```

```bash
# macOS / Linux
bash scripts/setup-worker.sh install
```

三个系统的共同底层命令是 `python3.13 scripts/setup-worker.py install`（Windows 可用 `py -3.13`）。如果 Python 未在常见路径中，可以通过 `PYTHON` 指定解释器的完整路径。已有 `.venv-portable` 只有在安装器的归属标记匹配时才复用；别的环境、链接目录、错误架构或 GPU 环境会被拒绝。中途中断后修好报错再重跑；不会删除已有环境来“修复”。需要并行保留旧环境时，对所有命令传相同的 `--venv /path/to/new-environment`。

安装末尾同时执行 `pip check` 和真实导入，不能只因依赖 metadata 已存在就成功。若 DLL/包文件缺失，先排查该目录的权限或占用；修好后使用 `install --repair` 重新安装此独立 profile 的 wheel，或选择新环境。不要修改系统防护/ACL 或结束无关进程来绕过失败。

成功后会新增两个不覆盖已有文件的本机配置：

- `.env.portable.local`：本地工作台，不需要账号。
- `.env.portable`：私有节点，连接与登录字段留空。每份新配置生成独立的随机 `NODE_ID`、`NODE_RUNTIME_DIR`、`NODE_TOKEN_FILE`、日志及工作目录，不使用 hostname 或另一台机器的缓存。

两份文件都被 Git 忽略。POSIX 上新文件权限为 0600；Windows 使用当前用户目录的继承权限。**复制部署或更换账号时，重新分配唯一节点 ID 和独立状态目录，不复制令牌。** 不要恢复已吊销节点、修改旧节点归属或覆盖其缓存。

## 3. 离线诊断与模型准备

```powershell
.\scripts\setup-worker.ps1 doctor
.\scripts\setup-worker.ps1 doctor --load-models
```

```bash
bash scripts/setup-worker.sh doctor
bash scripts/setup-worker.sh doctor --load-models
```

默认检查平台、Python/native imports、实际可用 provider、已配置模型的文件存在性，以及下载工具与编码器。**文件存在只报告 `files_present_unverified`；加 `--load-models` 后真实加载成功才报告对应能力 `ready`。** 它不搜索默认 `.env` 或读取用户素材，不调用模型下载/认证/claim。显式选择分布式配置时，会读取该配置指定的缓存 JSON 核对节点/API 归属，但不输出秘密值，也不改写缓存。诊断失败不打印底层异常原文或配置值。

按 [本地模型清单](local-mode.md#放置模型)取得有使用许可的权重并放入对应目录。换脸需要身份模型和至少一个换脸模型；本地整图换脸与分布式 GIF 还需要人脸检测器。缺检测器时分布式静态裁剪换脸仍可能就绪，GIF 声明必须以加载结果为准。抠图需要对应 ISNet。增强权重与平台门限见[增强部署](distributed-mode.md#增强模型)。本工具没有自动模型下载入口；不从未知链接获取模型。

诊断 JSON 分别包含 `installation_ok`、`providers`、`capabilities`、`ready_capabilities`、`authentication` 和 `execution`。默认退出码 0 **只表示 Python 安装检查通过**，不代表模型、账号或线上媒体成功。需要机器检查配置中的全部能力时使用 `doctor --load-models --require-ready`；缺任一能力返回非零。`execution=not_started_by_doctor` 表示这个命令未启动服务，不是在断言机器上没有其他进程。

## 4. 手动启动本地工作台

```powershell
.\scripts\setup-worker.ps1 start
```

```bash
bash scripts/setup-worker.sh start
```

打开终端显示的 `http://127.0.0.1:8765`；可用 `--port 8766` 改端口。先按本机 provider、模型文件和媒体工具筛选，再真实加载引擎。缺增强不会挡住其他已就绪能力，未启用的工具显示不可用。Ctrl+C 停止，不安装后台服务。

## 5. 私有分布式节点

管理员提供 Worker API origin、拥有 Worker 权限的账号及**永久密码**、匹配的 Cognito Pool/Client/Region 和允许的能力。在 `.env.portable` 的本机编辑器中填写；默认 `NODE_TOKEN` 留空，由 Worker 登录并自动刷新，不能把另一台电脑的短期节点令牌作为长期部署配置。账号必须允许节点登记；仅有 Cognito Worker 组身份并不代表账号策略、能力、节点归属都已通过授权。API origin 不一定是网站首页的 origin。

只启用管理员已授权且本机可用的 `CAPABILITIES`。用显式配置进行离线检查：

```bash
python3.13 scripts/setup-worker.py doctor --mode distributed --env-file .env.portable --load-models
python3.13 scripts/setup-worker.py start --mode distributed --env-file .env.portable
```

Windows 同样使用 `setup-worker.ps1` 包装器传这些参数。已有安全配置也可以明确传 `--env-file /path/to/your.env`，但必须有独立的 `NODE_ID`、`NODE_RUNTIME_DIR` 和位于该目录内的 `NODE_TOKEN_FILE`。doctor 会检查缓存身份/API 是否冲突，绝不清空或改写缓存；它不会登录，因此不能确认密码或节点是否已被撤销。

手动 `start --mode distributed` 才会登录、登记并领取生产任务；不要把它当离线验收命令。启动只注册实际加载的能力，再与服务端返回的授权能力取交集。无可用且获授权的能力就停止，不能通过修改本机能力绕过服务端策略。保留三槽、下载最多一个、租约/fencing 及 API 协议。

| 已观察到的认证错误 | 处理 |
| --- | --- |
| `FORCE_CHANGE_PASSWORD` / `NEW_PASSWORD_REQUIRED` | 先完成永久密码设置或 Cognito 新密码流程，不循环尝试临时密码 |
| 401 / `NotAuthorizedException` | 检查账号、永久密码及其 Cognito 环境 |
| 403 / `worker_access_not_authorized` | 管理员检查账号启用、Worker 组、登记策略、允许能力，以及节点归属/吊销状态 |
| `/api/worker/login` 返回 404 | Worker API origin 配错；不要从网站首页或管理页推导 API origin |
| 本地缓存冲突 | 保留旧文件，使用明确的新节点 ID 和独立状态目录，不复用他人或已吊销节点 |

可将已观察到的**错误码**交给离线 doctor 解释，例如 `doctor --mode distributed --auth-error 403`，或 `--auth-error FORCE_CHANGE_PASSWORD`；这不会验证账号、发送凭据或联系服务器。不要把密码、token 或完整错误正文当参数。

## 验证边界与来源

CI 矩阵覆盖 Windows x64、macOS M 系列原生 ARM64、Linux x64/ARM64 的安装、导入、doctor 及离线回归，包括临时生成的最小 ONNX 模型真实执行 CPU `Session.run` 并比对结果数组；不再包含 Intel Mac。它验证 native 运行库的基本执行，不代表实际换脸/抠图质量、商用模型兼容或 CoreML/GPU 加速。**workflow 已添加不等于 CI 已通过；wheel 可解析不等于 M 系列 Mac 原生媒体推理成功。** 具体通过情况以对应提交的 CI 结果为准；作业排队未取得机器时，不能宣称 ARM64 已验收，也不把旧提交或被取消的 CI 算作本次证据。

2026-11-07 的验证记录：此前 `b5104d1` 的 M 系列作业因 GitHub hosted ARM64 容量限制未取得机器，最终被取消，没有执行安装脚本。它不能作为 M 系列原生验收通过或脚本安装失败的证据；本次支持范围收口不改变这个验证边界。

2026-11-06 核对的官方依据：

- [PyPI ORT 1.23.2 文件](https://pypi.org/project/onnxruntime/1.23.2/#files)、[OpenCV 4.12.0.88](https://pypi.org/project/opencv-python/4.12.0.88/#files)、[NumPy 2.2.6](https://pypi.org/project/numpy/2.2.6/#files)、[InsightFace 2.0](https://pypi.org/project/insightface/2.0/#files)：普通 CPython 3.13 对应 wheel。
- [Python macOS 官方安装说明](https://docs.python.org/3.13/using/mac.html)：universal2、证书配置及不要修改系统 Python。
- [Homebrew 安装要求](https://docs.brew.sh/Installation)：当前系统及 Apple Silicon 支持条件。
- [ONNX Runtime CoreML](https://onnxruntime.ai/docs/execution-providers/CoreML-ExecutionProvider.html)：provider 的要求与算子支持边界。
- [GitHub runner 矩阵](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)：实际 OS/架构仍由 CI 断言核对，防止标签变更。
