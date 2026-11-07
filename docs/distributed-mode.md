# 私有分布式部署

节点只主动连接 Web-ClapClip 中央后台 `https://www.clapclip.net`，由它统一管理五站任务；Worker 不直连业务站 API。后台签发的存储链接使用独立匿名客户端下载输入、上传结果，不携带节点 token 或 cookie，不需要数据库账号、AWS密钥或入站端口。

新的[无人值守安装候选](one-click-contract.md)实现无参安装、后台开机自启、整机单实例及排空更新。新 managed 配置只对接 Web-ClapClip 主后台，不直连业务站 API；主后台契约验收前明确停止安装。候选的本地验证不代表线上服务就绪，也不迁移现有手工配置或生产实例。

首次部署优先使用[跨平台安装、离线 doctor 和手动启动](portable-setup.md)，同一套逻辑适用于 Windows、macOS 和 Linux。它生成独立节点/状态目录，保留已有配置，默认不登录、不下载模型、不启动或设置自启。下面的 DirectML/后台管理命令用于明确选择该部署方式的管理员，不是 portable 安装器的默认步骤。

## Windows：一个实例提供四项能力

使用 Python 3.13、Git、Node.js 22+、FFmpeg 和 FFprobe。先建立新的 DirectML 环境：

```powershell
py -3.13 -m venv .venv-unified
.venv-unified\Scripts\python -m pip install -r requirements-unified-windows.txt
.venv-unified\Scripts\python -m pip install --no-deps -e . "insightface==2.0"
Copy-Item .venv-unified\Scripts\python.exe .venv-unified\Scripts\ClapClipWoker.exe
Copy-Item .env.example .env
```

编辑 `.env`：保持 `API_BASE_URL=https://www.clapclip.net`，填写节点身份和匹配的 Cognito 配置，或使用本机已有 `NODE_TOKEN`。启用 `CAPABILITIES=faceswap,cutout,enhance,download`，设置 `CONCURRENCY=3`。账号及节点需在中央后台获准使用相应能力。密码只由 Cognito SDK 发送，中央后台收到 ID token；勿把密码放入命令行或提交。

按[本地部署](local-mode.md#放置模型)准备换脸和抠图模型，增强模型见下表。首次启动可下载经过哈希验证的缺失换脸/抠图权重；使用自己的私有模型仓库时填写只读 `GITHUB_TOKEN`。公开模型仓库无需token。增强权重需自行安装。

```powershell
.venv-unified\Scripts\ClapClipWoker.exe -m mediaworker run --all-capabilities
```

四引擎全部加载后才认证上线。三个AI轮询器和一个下载轮询器共享最多三个OS执行槽，下载最多一个任务；不要另起下载进程。DirectML与CUDA不能装在同一个环境；CUDA环境按 `windows-cuda` 依赖组另建，增强只支持DirectML/CUDA。

### 独立后台常驻

先结束前台实例并等待在途任务完成，再注册统一监督器：

```powershell
.\scripts\register-unified-worker-windows.ps1
Start-ScheduledTask -TaskName MediaWorker
Get-ScheduledTask -TaskName MediaWorker
```

任务独立于终端，故障一分钟重启，无运行时长限制，不带开机/登录触发，需要用户保持登录。已有任务默认拒绝覆盖；仅在核对身份、排空任务并停止旧监督后使用 `-ReplaceExisting`。停止计划任务可能留下业务子进程，不能只按进程名称强杀或再次启动副本。

就绪信息在 `logs/mediaworker.log`，监督日志为 `logs/supervisor-run.log`。四项 `capability ready` 和四个 `worker ... online` 表示启动完成；成功HTTP轮询默认不写日志，日志静默不等于掉线。启动就绪不证明真实媒体任务已成功。

## macOS（仅 M 系列）/ Linux

使用普通原生 Python 3.13；Mac 仅支持 M 系列的原生 arm64 Python（universal2 也必须以 arm64 运行），拒绝 Intel 与 Rosetta。运行 `bash scripts/setup-worker.sh install`、`bash scripts/setup-worker.sh doctor --load-models`；填写独立的 `.env.portable` 后，明确运行 `bash scripts/setup-worker.sh start --mode distributed --env-file .env.portable` 才会连接私有队列。平台、wheel 与系统工具要求见[支持矩阵](portable-setup.md#支持范围)。按实际加载结果及服务端授权启用能力；CPU/CoreML 不满足当前增强引擎的 DirectML/CUDA 要求。

旧入口 `bash scripts/install-mac.sh` 现在只调用共享安装器，**不再安装或启动 LaunchAgent**。`scripts/uninstall-mac.sh` 仅供主动移除历史 LaunchAgent，不参与新安装。后台服务需要管理员另行选择并授权，安装成功不能当作已常驻或已上线。

## 增强模型

放入 `ENHANCE_MODELS_DIR`，仅成功加载的模型会被发布：

| 模型 | ONNX文件 | 倍率 |
| --- | --- | --- |
| standard | RealESRGAN_x2plus_512_fp32.onnx | 1/2/4 |
| fast | span_liveaction_x2_704_fp16.onnx | 1/2/4 |
| deblur | nafnet_deblur_512_fp32.onnx | 1 |
| animation | realcugan-pro-conservative-up2x.onnx、realcugan-conservative-up4x.onnx | 1/2/4 |
| portrait | yoloface_8n.onnx及所选人像模型 | 1/2/4 |

`face=gfpgan`使用 `gfpgan_1.4.onnx`，`face=gpen`使用 `gpen_bfr_256.onnx`，均需检测模型；`model=portrait`必须启用face。增强输入为单帧JPEG/PNG/WebP，输出透明度保留、方向归正且移除元数据的PNG。4倍可由两次2倍推理实现。完整图片处理可直接使用本地工作台验证，无需领取队列任务。

### 人像任务的领取声明

包含 `enhance` 的 `POST /api/worker/claim` 请求额外发送 `faceModels: ["gfpgan", "gpen"]`，列表仅包含当前引擎成功加载、且检测器就绪的人脸修复模型；没有可用模型时显式发送 `[]`。它与原 `models` 并列，`models` 的内容和已有任务字段不变。下载及其他不含 `enhance` 的领取请求不携带此字段。

服务端应把 `faceModels` 作为可选字符串数组接收；旧客户端缺失、空数组或未知值均不能证明指定人脸修复模型可用。分配 `params.face != "off"` 的增强任务时，除原有权限、能力、基础 `params.model` 匹配外，必须确认该次领取的 `faceModels` 包含所需的 `gfpgan` 或 `gpen`。不能仅凭 `models` 中的 `portrait` 分配 `standard + gfpgan`，也不能用历史节点记录替代当前领取声明。普通无脸部修复任务维持原调度。声明只描述就绪情况，不能提升节点授权。

例如，整图增强加温和人像修复可使用 `params={"model":"standard","scale":2,"face":"gfpgan","faceStrength":0.5,"faceEdgeBlend":0.4,"faceFusionArea":50}`，并匹配 `models` 含 `standard` 且 `faceModels` 含 `gfpgan` 的领取请求。强度 `0.5` 是产品候选值，仍需真实视觉样本确认。

现有 heartbeat 是单个已领取任务的租约续期，不是节点模型清单；它和 complete/fail 的字段、租约及 fencing 规则保持不变。服务端应先兼容可选 `faceModels` 并启用上述调度约束，再更新节点运行源，最后开放人像入口；旧节点仍可领取其他兼容任务。

## 配置与更新

`.env.example`列出连接、模型、路径和限值配置。下载另外需要本地编码器，见[下载能力](download-queue.md)及[协议](download-policy.md)。换脸私有任务使用浏览器对齐的人脸裁剪，原生尺寸为Baseline 128×128、HyperSwap 256×256；检测及最终融合由前端完成。本地工作台则在本机完成整图检测和融合。

默认关闭自动更新。显式启用 `WORKER_AUTO_UPDATE=true` 时，只接受配置远端/分支的快进提交，结束在途任务后退出，由监督器重启。不重置本地修改。固定运行副本不自动跟随开发目录，提交也不等于运行上线。

队列保留认证轮转、租约心跳、过期结果fencing和独立attempt存储；不能跳过后台维护任务或把失败轮询当作可盲目重放的请求。开发和本地验证见[架构说明](architecture.md)。
