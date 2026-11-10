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

本次info可在私有`options[formatId].browserDirect`附三个字段`{url,expiresAt,ext}`，其中expiresAt为ISO UTC、本次入口最长60秒的候选有效期，可识别的源URL到期提示只能缩短它；不保证源URL能被撤销、尚未失效或支持浏览器CORS/地区/IP。仅从本次final-video/已选单资源中筛选声明同时含音视频的HTTPS progressive MP4/WebM，不支持分轨、静音、未知codec、DRM、manifest或凭据/自定义请求头；标准浏览器自带UA/Accept/语言及yt-dlp默认Sec-Fetch-Mode提示可忽略但不向前端传任何headers，实际是否必需仍由匿名fetch结果决定。源URL显式声明签名所需headers时，仅host可候选，其余omit；URL原字节不改写，也不验证密钥或签名。大尺寸/长来源不因此拒绝或强制Worker，浏览器须独立demux、裁切/缩放并验证真实输出；无法处理时沿用原Worker，媒体无固定体积拒绝。

这个hint不是已验证CDN链接：Worker不为它增加DNS/HEAD/媒体请求，不改变40秒inspect预算。主站必须在owner-bound私有投影前完成到期、目标准入及DNS所有地址公网检查，门未实现或不通过时返回无候选；不能将其放进public formats、raw info出口、admin DTO、日志或事件。浏览器仅以`credentials:omit`、`redirect:error`、`referrerPolicy:no-referrer`的真实CORS fetch尝试，不能把URL当裸导航或跟随未校验重定向，也不能声称继承Worker的socket/DNS pinning。媒体入队时主站应pick原有选项字段去掉candidate URL；Worker仍fresh resolve并丢弃该附加字段，不将它作为取源入口。URL仅供短期私有投影，完整原始extractor info不持久化。

媒体必须实际处理/截取，最终ffprobe核对输出duration和尺寸：显示方向含旋转及像素比例归正后，不放大、等比例放入任务包围盒；只有高分辨率源时允许本地缩小，音频组件不套视频过滤。MP4为H.264/AAC，WebM为VP9/Opus，纯音频MP3。编码预留0.1秒处理容器舍入，结果严格<=任务maxDuration，无超限容差。

MP4仅在本地实际probe确认H.264 8bit yuv420p、兼容profile/level、AAC-LC（若选择含音频）、已合规偶数尺寸、SAR1、rotate0、展示起点0及有效整数时间戳时尝试从头streamcopy合并/裁切。copy结果必须重新核对每条stream及容器时长、所选尺寸和完整音视频解码；不符合即保留源文件回退原转码。低帧率/长GOP的copy裁切可能越界，不因参数设了300秒而放行。取消/超时不触发回退，全部阶段共用原deadline；MP3/WebM保持原转码。此路径减少符合条件媒体的本地编码工作，不保证来源传输、上传或端到端更快，也不承诺只下载前300秒来源字节。

媒体complete在原node_id/job_id/lease_token/result_key/w/h之外必须有真实ffprobe输出duration。视频w/h为实际显示像素，MP3为0x0。没有历史媒体1x1无duration兼容；AI完成字段、模型及输入/输出大小保护不变。服务端仅持久化经过验证的输出字段，不由完成请求自报标记切换任务策略。

## 传输与保护

媒体任务仅复用本次 attempt 刚解析并重新核对所选组件的最终 video 元数据；组件下载不再重新提取网页，失效时失败闭合，不回退到排队时的旧 URL 或隐式网页刷新。跨进程数据按必要字段白名单重建，移除 cookies、认证头、额外 URL、执行状态及路径；signed URL 只留在当前 attempt 的最多 8MiB 私有临时 JSON，成功、失败、取消时均清理，不入队列、日志或发布包。组件仍走受控 native downloader，HTTP 分块及必要 HLS/DASH 字段保留，FFmpeg 只处理本地文件或受控管道；自定义 AES/downloader 覆盖不进入此桥接。超时诊断区分 info、download_source、probe、transcode 和 output_probe，既有超时及输出规则不变。

超过输出时长的HTTP(S) MP4/M4A组件，若有已知音视频身份且moov在mdat之前，按同一0..任务maxDuration窗口顺序取前段：当前native HttpFD保留原小块HTTP请求、公开连接代理及重定向保护，将纯二进制媒体经有界64KiB管道送给父进程直接启动的FFmpeg。FFmpeg输入只允许pipe协议、强制MOV解封装且禁用外部track/绝对路径引用，不接收来源URL，不联网取媒体，不强制重编码关键帧。首moov按box实际长度渐进读取且累计<=8MiB；mdat先到、未知codec身份或缺乏可信分片证据等不支持路径明确记录full_native回退，继续共用原deadline。合法chunked来源无Content-Length仍可使用pipe，内部source_bytes=0/source_bytes_known=false仅表示未知，不增加源大小准入。已安全打开的body读取中断/已知长度短读、后续Range被忽略等需要native重置文件的情况，先完整回收双子进程/线程并删partial，再以同fresh元数据、同format、同deadline执行一次原native文件路径，不复inspect或增加其重试参数；编码不受pipe支持时同样明确回退。权限、egress、格式安全拒绝及真正终态进程错误仍失败，取消/超时优先且不触发回退。已完成的consumer不足以证明成功；子进程和读写线程未确认全退时，执行future、共享槽和attempt归属保持到回收完成。每个组件仅在内部日志记录策略、同窗口、格式保持布尔、上游响应body的应用读取字节（非wire精确流量）及源总长度；指标来自随机私有文件和nonce，不相信来源stderr。预读或小音轨可能读取更多甚至完整来源，不能把输出300秒等同仅下载300秒字节；实际最终时长、每条stream、映射与完整解码门仍由本地处理核验。

媒体HTTP 5xx仍由原HttpFD按既有重试次数处理；成功恢复的响应不锁死pipe失败，耗尽或尚未恢复的失败不会被consumer完成/主动停源掩盖，没有新增重试循环。

媒体源文件、中间文件、结果及其上传均没有应用固定字节上限，格式列表无大小提示；旧DOWNLOAD_MAX_BYTES/MAX_DURATION/MAX_WIDTH/MAX_HEIGHT环境值不再消费，也不换成更大的隐藏上限。允许在任务bounds中合法收紧尺寸/时长，与本地旧环境策略无关。

HTTP decoded元数据及HTTPError正文每响应<=8MiB；普通info/文本子进程stdout/stderr合计<=8MiB，section路径仅累计诊断stderr<=8MiB，二进制媒体stdout不计入文本上限；info JSON结果<=512KiB。section媒体以64KiB阻塞读写背压、最多8MiB前置元数据和同一deadline约束，无隐藏媒体总字节cap。只有HttpFD媒体/fragment流不受元数据读取上限约束，不依赖来源Content-Type；inspection proxy有界，进入媒体阶段才解除媒体隧道体积限制。SSRF、公网DNS/连接pinning、来源准入、认证、socket并发、超时、限速、租约/fencing/ACK、取消清理和共享三槽/下载一个poller保留。来源由受控native yt-dlp下载，ffmpeg只读本地文件或受控管道；超长/慢来源仍可超时，不承诺只向源站请求前300秒字节。

## 发布边界

前置URL准入的结构化INVALID_URL/UNSUPPORTED_SITE保留安全公开码，不再因英文message退为UNKNOWN_FAILURE。egress拒绝行为仍为原403/407，端口、DNS全地址公网检查、连接pinning、代理认证、请求及重试预算不变。内部按每次runner调用/phase建立独立receipt，请求在accept时绑定所属scope；只记录固定stage/reason与有界计数（每reason最多1000），不记目标、IP、URL、headers或异常原文。observed_rejections是观察集合，可能包含后续已恢复的子请求；returned/raised只描述runner调用结果，不能把早先某次拒绝当整个任务最终失败原因，也不继承到下一次调用。历史事件无法靠这些新增分类追认具体原因，分类修复不保证来源失败率全部消失。

下载内部阶段日志仅含固定枚举和数值，不含任务身份、来源URL或headers：info、source、source_probe、copy、encode、decode、output_probe、engine、preupload、upload、complete各记begin/end及monotonic耗时，source带原生protocol枚举，engine/upload记本地输入输出字节。现有300秒仍是fresh inspect之后的处理预算；preupload心跳、上传和complete ACK在该预算之外，上传120秒是每I/O超时且保留原重试，日志不将这些时段冒称编码耗时。

原生分片前缀只在本次fresh/同format下启用：HLS读取一次受原代理与8MiB元数据限制的VOD media playlist，以可信EXTINF保留从首片到覆盖窗口的完整原行前缀，KEY/MAP/BYTERANGE/MEDIA-SEQUENCE及默认IV处理继续交原HlsFD。普通缓存、绝对时钟和起始discontinuity序号标签原样保留；真实discontinuity、master/live/START/LL/ad/GAP、缺时长或未知EXT标签明确full_native。实际媒体前缀识别TS或moov在前的MOV后才交固定pipe解封装，不把无MAP推断为TS。

无时长的http_dash_segments仅在fresh精确整数filesize与同selected baseURL的唯一range参数证明从byte0连续、无缺口/重叠直至EOF时使用原Dash producer→MOV pipe，以实际PTS裁取，不按字节/片数估时；官方末片end=filesize的一过尾拼法仅归一为最后有效字节，空EOF附片拒绝。带Content-Range的响应须与该片声明、片内resume及精确总长一致，原文件中的重绕/恢复由native负责；未知filesize、近似size、无法证明初始化归属的普通duration DASH等明确full_native。选中前缀缺片不允许skip后假成功，原生安全重试保留；取消/超时/未恢复错误不能被主动停源掩盖。

每个组件使用同一0..limit窗口、同deadline且fragment并发1。原FragmentFD先完整下载一片到私有临时文件再读入管道，因此预读上界含一个完整原生片及native/OS缓冲，不能宣传只有64KiB或精确到300秒的网络流量。内部fragments_started是HttpFD片尝试（含初始化和重试），不是唯一媒体片数；read_bytes仅计响应body应用读取，HLS无整源长度时source_bytes=0/unknown，严格byte-dashy才标fresh_filesize。Windows仅当Python启动目标精确等于当前sys.executable时，以受信base interpreter和重新构造的当前venv hint取得actual进程句柄；显式其它Python保留原native路径，不偷换解释器。消费者停止后原生迭代器先检查intent-stop，进程、线程、私有分片目录均确认收口后才回业务/释放槽；没有新增FFmpeg网络协议、外部下载器、网页重提取、媒体字节cap或公开业务字段。

代码提交、预检或本地生成样本不证明已上线。Download与统一CutOut API的生成/分配/完成验收、数据库约束、对象传输和当前Worker都要一致；应用无cap不等于生产数据库已解除固定体积约束。必须逐节点核实实际加载能力，不能以本机推断其它节点。
