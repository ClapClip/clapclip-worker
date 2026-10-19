# 两种部署方式

| | 私有分布式 | 本地工作台 |
| --- | --- | --- |
| 启动 | `python -m mediaworker run --all-capabilities` | `python -m mediaworker local` |
| 使用入口 | 自己的网站和私有任务 API | `http://127.0.0.1:8765` |
| 账号 | 自己部署的后端提供节点认证 | 不需要账号 |
| 素材 | 从私有队列领取、处理并上传结果 | 浏览器上传至本机，结果从本机保存 |
| 联网 | 访问私有 API、存储和允许的公开下载源 | 图片处理不联网；视频下载需要联网 |
| 配置 | `.env` | 默认不读 `.env`；可显式指定 `.env.local` |

私有模式沿用现有注册、租约、队列、存储协议；本仓库提供计算节点，不包含网站后端的完整部署。不要把已有生产账号或密钥放入开源仓库。

## 本地安装

建议 Python 3.13。先在项目目录建立环境：

```powershell
# Windows / DirectML
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-unified-windows.txt
.venv\Scripts\python -m pip install --no-deps -e . "insightface==2.0"
```

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[downloader]'
```

下载功能还需要 FFmpeg、FFprobe 和 Node.js 在 PATH 中，具体要求见下载部署说明。CUDA 用户按现有 CUDA 部署说明安装对应运行环境，勿同时安装多套 ONNX Runtime。图片引擎仍沿用各自硬件要求：换脸检测支持已安装的 ONNX provider；当前画质增强仅支持 DirectML 或 CUDA，因此 macOS 上该工具会显示未就绪，其余工具可独立使用。

Windows使用上述顺序，是为了避免InsightFace声明的CPU版 `onnxruntime` 覆盖DirectML。`--no-deps`步骤之前必须完成完整requirements安装。此时 `pip check` 可能仍提示缺少名称为 `onnxruntime` 的发行包，实际导入由DirectML发行包提供；不要因此把CPU版装回同一环境。检查 `onnxruntime.get_available_providers()` 应包含 `DmlExecutionProvider`。

## 放置模型

本地网页不自动下载未知来源权重。安装具有使用许可的模型，默认位置如下，也可复制 `.env.local.example` 为 `.env.local` 修改目录：

源码的MIT许可不覆盖这些权重；商用或转发前请阅读[第三方模型说明](../THIRD_PARTY.md)。模型未装齐时，网页仍可启动并显示哪些工具尚未就绪。

```text
~/.mediaworker/models/
  arcface_w600k_r50.onnx
  inswapper_128_fp16.onnx        # Baseline；也支持 inswapper_128.onnx
  hyperswap_1a_256.onnx          # Robust，可选
  hyperswap_1b_256.onnx          # Balanced，可选，安装后优先选择
  hyperswap_1c_256.onnx          # Accurate，可选
  cutout/isnet-general-use.onnx
  enhance/yoloface_8n.onnx      # 整图换脸也需要这个检测模型
```

换脸至少需要身份模型、一个换脸模型和检测模型。画质增强模型沿用 `ENHANCE_MODELS_DIR`、`ENHANCE_MODELS`、`ENHANCE_FACE_MODELS` 配置，文件清单见增强引擎与现有部署文档。未装齐的工具显示未就绪，不影响网页及其他工具启动。只有已成功加载的模型出现在下拉框。启动后加载一次，不在每个任务重复加载。

## 启动和使用

```powershell
# Windows
.\scripts\start-local.ps1
# 使用自己的模型目录等配置
.\scripts\start-local.ps1 -EnvFile .env.local
```

```bash
# macOS / Linux
bash scripts/start-local.sh
# 自定义端口和本地配置
bash scripts/start-local.sh --port 8766 --env-file .env.local
```

打开终端显示的 `http://127.0.0.1:8765`。换脸上传目标照片和来源照片，自动使用两张照片中最大且可对齐的人脸，按模型原生 128/256 尺寸裁剪，再把结果融合回完整目标图。去背景输出透明 PNG。增强选择模型及倍率。下载先解析链接，再选择可用格式下载。可取消任务、保存结果或清理单项结果。

关闭终端或按 Ctrl+C 停止工作台；停止会等待已进入推理的任务退出，再清理本次任务文件。运行中的 ONNX 推理不能立即中断；取消后不会提供其结果。强制结束进程可能留下 `~/.mediaworker/local/` 内的临时素材，需要自行清理。结果保留最长一小时，在下一次状态查询/提交时清理；单次会话最多保留 32 个任务、最多 9 个等待或处理中的任务。

服务固定监听 127.0.0.1，带本机 Host/Origin 检查和会话操作令牌，不提供局域网发布参数。请通过终端给出的精确地址访问，不通过公网反向代理暴露。图片只传给本机；视频下载复用受控公开来源校验，不读取 Cookie，不支持任意网址或绕过限制。

本地和私有模式应在不同部署上使用，同一机器不要同时加载两套模型。处理仍使用原有共享三个 OS 槽，本地下载同时最多一个。模型内部锁可能让同一模型的推理串行；三个执行槽不代表三个 GPU 模型一定同时计算。此版本没有改动生产 Worker 的常驻任务或自动更新配置。
