# ClapClip Worker

源码采用 [MIT](LICENSE) 许可证。模型权重不随项目发布，其使用与商用授权见[第三方说明](THIRD_PARTY.md)。

支持换脸、抠图、画质增强和视频下载，提供两种运行模式。

| 模式 | 适用场景 | 入口 |
| --- | --- | --- |
| 本地工作台 | 在自己的电脑上传素材、预览和保存结果，无需账号 | `python -m mediaworker local` |
| 私有分布式 | 多台计算节点连接自己的后台，领取任务并回传结果 | `python -m mediaworker run --all-capabilities` |

## 本地使用

建立 Python 环境并安装依赖、准备模型后运行：

```bash
python -m mediaworker local
```

浏览器打开 `http://127.0.0.1:8765`。本地模式不读取默认 `.env`，不注册节点、不领取远程任务。图片在本机处理，视频下载需要联网。

完整安装步骤、模型清单和平台限制见[本地部署](docs/local-mode.md)。Windows / macOS / Linux 启动脚本分别是 `scripts/start-local.ps1` 和 `scripts/start-local.sh`。

## 私有部署

复制 `.env.example` 为 `.env`，填写自己的 API、认证和模型来源配置，准备好运行环境与模型后运行：

```bash
python -m mediaworker run --all-capabilities
```

同一实例提供四项能力，共享最多三个执行槽，下载同时最多一个任务。本仓库提供计算节点，网站后端需要自行部署。详见[分布式部署](docs/distributed-mode.md)。

## 文档

- [本地工作台](docs/local-mode.md)
- [模型、GPU、认证与节点维护](docs/distributed-mode.md)
- [下载队列](docs/download-queue.md)与[下载规则](docs/download-policy.md)
- [架构与开发](docs/architecture.md)
- [安全与源码发布](SECURITY.md)
- [参与开发与发布检查](CONTRIBUTING.md)

## 开发

唯一开发仓库是 [ClapClip/clapclip-worker](https://github.com/ClapClip/clapclip-worker)，所有开发在 `main` 完成。原 MediaWorker 仓库停止维护，后续功能与修复统一提交到这里。操作约定见 [AGENTS.md](AGENTS.md)。

```bash
MEDIAWORKER_LOAD_DOTENV=0 python -m unittest discover -s tests -v
```

源码在 `mediaworker/`，测试在 `tests/`，部署脚本在 `scripts/`。模型、环境、日志和构建产物均不进入版本控制；不要提交真实凭据或用户素材。
