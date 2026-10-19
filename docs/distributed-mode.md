# 私有分布式部署

节点主动连接自己的 Worker API，通过预签名地址下载输入、上传结果，不需要数据库账号、AWS密钥或入站端口。后台需自行部署匹配的认证、任务、租约与存储协议。

## Windows：一个实例提供四项能力

使用 Python 3.13、Git、Node.js 22+、FFmpeg 和 FFprobe。先建立新的 DirectML 环境：

```powershell
py -3.13 -m venv .venv-unified
.venv-unified\Scripts\python -m pip install -r requirements-unified-windows.txt
.venv-unified\Scripts\python -m pip install --no-deps -e . "insightface==2.0"
Copy-Item .venv-unified\Scripts\python.exe .venv-unified\Scripts\ClapClipWoker.exe
Copy-Item .env.example .env
```

编辑 `.env`：填写自己的 `API_BASE_URL`、节点身份和 Cognito 配置，或使用已有 `NODE_TOKEN`。启用 `CAPABILITIES=faceswap,cutout,enhance,download`，设置 `CONCURRENCY=3`。账号及节点需在后台获准使用相应能力。密码只保存在本机，勿放入命令行或提交。

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

## macOS / Linux

Python 3.13 环境安装 `pip install -e '.[downloader]'`，复制并填写 `.env` 后运行 `python -m mediaworker run --all-capabilities`。只在 `CAPABILITIES` 启用本机支持且装齐模型的能力；macOS目前不支持增强引擎。macOS可使用 `bash scripts/install-mac.sh` 安装登录后常驻服务，`bash scripts/uninstall-mac.sh` 移除该服务。Linux使用自己的服务管理器管理同一入口。

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

## 配置与更新

`.env.example`列出连接、模型、路径和限值配置。下载另外需要本地编码器，见[下载能力](download-queue.md)及[协议](download-policy.md)。换脸私有任务使用浏览器对齐的人脸裁剪，原生尺寸为Baseline 128×128、HyperSwap 256×256；检测及最终融合由前端完成。本地工作台则在本机完成整图检测和融合。

默认关闭自动更新。显式启用 `WORKER_AUTO_UPDATE=true` 时，只接受配置远端/分支的快进提交，结束在途任务后退出，由监督器重启。不重置本地修改。固定运行副本不自动跟随开发目录，提交也不等于运行上线。

队列保留认证轮转、租约心跳、过期结果fencing和独立attempt存储；不能跳过后台维护任务或把失败轮询当作可盲目重放的请求。开发和本地验证见[架构说明](architecture.md)。
