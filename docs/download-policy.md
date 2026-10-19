# 当前下载规则与内部协议

下载只有一套当前规则：最高1280×720显示包围盒，视频与音频从0输出最多300秒，媒体无固定体积上限。用户不选择政策版本。数字2仅为内部wire能力标记。

## 领取与过期

专用下载claim发送 `downloadPolicyVersions:[2]`；AI claim不加字段。每次claim必须明确支持当前标记才能获派当前任务，缺声明/仅旧能力不能默认得到任务。任务保存自身limits，Worker不替旧任务改策略，不升级/重放/清库/改账本。

新任务默认生成恰有下列七字段：

```json
{"version":2,"maxHeight":720,"maxWidth":1280,"maxDuration":300,"maxBytes":null,"requireDuration":true,"requireKnownHeight":true}
```

执行/验收允许同一当前标记内更严的任务bounds：maxWidth、maxHeight、maxDuration必须是正safe整数，且分别<=1280/720/300；Python bool和float不当整数。version严格整数2、flags严格true、maxBytes仅null，恰有七字段。超顶、缺/旧/未知/额外字段一律INVALID_LIMITS，不clamp或升级。网站对过期媒体选择HTTP410 POLICY_EXPIRED、非法新请求HTTP422 INVALID_LIMITS；需要重新解析建立新的合法选择，不自动重放旧任务。

## info与媒体

来源必须是公开单个录播，duration已知、有限且>0，可长于300秒；视频轨必须有已知尺寸，纯音频不要求宽高。拒DRM、直播/预告、playlist，不用cookies。

info JSON中的duration保留来源时长，结果<=512KiB。info complete仍w=1,h=1、无duration，这是元数据动作设计。

媒体必须实际转码/截取，最终ffprobe核对输出duration和尺寸：显示方向含旋转及像素比例归正后，不放大、等比例放入任务包围盒；只有高分辨率源时允许本地缩小，音频组件不套视频过滤。MP4为H.264/AAC，WebM为VP9/Opus，纯音频MP3。编码预留0.1秒处理容器舍入，结果严格<=任务maxDuration，无超限容差。

媒体complete在原node_id/job_id/lease_token/result_key/w/h之外必须有真实ffprobe输出duration。视频w/h为实际显示像素，MP3为0x0。没有历史媒体1x1无duration兼容；AI完成字段、模型及输入/输出大小保护不变。服务端仅持久化经过验证的输出字段，不由完成请求自报标记切换任务策略。

## 传输与保护

媒体源文件、中间文件、结果及其上传均没有应用固定字节上限，格式列表无大小提示；旧DOWNLOAD_MAX_BYTES/MAX_DURATION/MAX_WIDTH/MAX_HEIGHT环境值不再消费，也不换成更大的隐藏上限。允许在任务bounds中合法收紧尺寸/时长，与本地旧环境策略无关。

HTTP decoded元数据及HTTPError正文每响应<=8MiB，子进程stdout/stderr合计<=8MiB，info JSON结果<=512KiB，三者分别保留。只有HttpFD媒体/fragment文件流不受元数据读取上限约束，不依赖来源Content-Type；inspection proxy有界，进入媒体阶段才解除媒体隧道体积限制。SSRF、公网DNS/连接pinning、来源准入、认证、socket并发、超时、限速、租约/fencing/ACK、取消清理和共享三槽/下载一个poller保留。来源由受控native yt-dlp下载，ffmpeg只读本地文件；超长/慢来源仍可超时，不承诺只向源站请求前300秒字节。

## 发布边界

代码提交、预检或本地生成样本不证明已上线。Download与统一CutOut API的生成/分配/完成验收、数据库约束、对象传输和当前Worker都要一致；应用无cap不等于生产数据库已解除固定体积约束。必须逐节点核实实际加载能力，不能以本机推断其它节点。
