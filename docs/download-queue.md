# 统一节点中的下载能力

私有分布式模式使用一个已安装下载依赖的 Python 3.13+ 环境：

```bash
python -m mediaworker run --all-capabilities
```

在 `.env` 中启用 `CAPABILITIES=faceswap,cutout,enhance,download`，使用同一个 API、节点身份和认证配置。三个 AI poller 和一个下载 poller 共享最多三个主机执行槽，下载同时最多执行一个任务。无需单独的下载进程、HTTP 服务或 API 密钥。后台常驻使用统一监督器，见[部署说明](distributed-mode.md)。

本地模式直接在工作台的视频下载页提交链接，安装与启动见[本地部署](local-mode.md)。它复用相同的下载引擎及来源检查，不连接私有队列。

## 运行依赖与配置

安装项目的 `downloader` 依赖组，并准备 Node.js 22+、FFmpeg 和 FFprobe。版本及编码器就绪检查由下载引擎启动时执行；缺少依赖时不能将下载能力标记为就绪。

可通过 `DOWNLOAD_FFMPEG`、`DOWNLOAD_FFPROBE`、`DOWNLOAD_NODE` 指定工具位置，`DOWNLOAD_JOB_TIMEOUT`、`DOWNLOAD_INFO_TIMEOUT` 控制超时，`DOWNLOAD_PUBLIC_DNS` 选择现有公网 DNS 解析路径。来源扩展使用明确的 `DOWNLOAD_EXTRA_ALLOWED_HOSTS`；SSRF、公网地址与代理检查始终保留。

## 任务与结果

任务限值、错误码和格式约束见[下载协议](download-policy.md)。默认视频包围盒为 1280×720，输出从 0 最多 300 秒，音频同样受时长限制；媒体没有固定字节上限。元数据大小、超时、取消与临时文件清理仍受控。

下载引擎通过受控出站代理访问允许的公开视频来源，FFmpeg 只处理本地文件，不读取浏览器 Cookie，不支持直播或播放列表。私有模式沿用认证、租约、心跳和 fencing，结果经预签名存储地址流式上传。轮询成功和依赖就绪不代表实际媒体任务成功；离线回归使用生成素材及替身，不默认提交生产任务。
