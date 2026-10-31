# Animated GIF face swap

Distributed GIF jobs use the existing `faceswap` task and shared execution slots.
They require **both** `params.mode = "gif"` and `params.targetMedia = "gif"`.
`inputs.target` contains original GIF bytes; `inputs.source` is a static,
browser-aligned face crop at the selected model's native 128 or 256 square size.
`model`, `edgeBlur` and `fusionArea` retain their meanings. Client frame counts,
dimensions and duration hints are never trusted.

Normal `mode="face"` still accepts only aligned static JPEG/PNG/WebP crops and
returns transparent PNG. It rejects GIF rather than decoding frame zero. The
standalone local workbench retains its static full-photo interface.

## Queue gate

Image pollers include top-level `faceswapMedia: ["image", "gif"]` only when
swap/identity models and the YOLO detector actually loaded and the detector
passed inference. Detector failure leaves static swaps available and declares
`["image"]`. Unloaded/mock engines never declare GIF support; download claims
omit this field. The server must require `"gif"` for GIF jobs and treat absence
as static-only. Other capability, model and policy declarations are unchanged.

## Limits and playback

The worker enforces the smaller of configured image limits and these hard limits:

| Resource | Limit |
| --- | --- |
| Compressed GIF input | 6 MiB |
| Canvas longest side | 1024 pixels |
| Frames | 2–300 |
| Total decoded canvas pixels | 60,000,000 |
| Playback duration | 30,000 ms |
| Individual effective delay | 20–10,000 ms |
| Encoded result | 32 MiB, also bounded by `MAX_RESULT_BYTES` |
| Validation + processing + encoding | 120 seconds cooperative deadline |

The hosted JSON endpoint may impose smaller limits (currently 2 MiB GIF,
512 KiB source, 4 MiB request); a worker limit is not an upload promise.
Missing, zero and 10 ms delays become 100 ms, matching the website's browser
playback policy. Other delays are retained; budgets use effective delays.
Loop absence remains absent; finite counts and zero (infinite) remain distinct.

A bounded compressed-block pass checks canvas, frame rectangles, counts, timing
and structure before any raster decode or inference. Every frame is decoded once
to reject corruption before GPU work. Unknown rendering extensions, out-of-canvas
rectangles, truncation and trailing data fail.

Pillow composites source disposal and transparency. Output uses independently
quantized full-canvas frames with clear-to-transparent disposal, retaining order
even for identical consecutive frames. GIF binary alpha is retained; opaque
colours use at most 255 palette entries plus a reserved transparency index. The
encoder keeps one frame, not an animation-wide pixel list. Metadata is omitted.

## Face selection and lifecycle

Only `faceSelection="primary"` (the default) is supported. The first usable
frame's largest face establishes a fixed identity reference. Later frames
consider at most eight faces, require cosine similarity at least 0.4 and a 0.05
lead over another candidate. Missing/ambiguous matches keep the frame unchanged.
An animation with no swapped frames fails. This is a single-person workflow,
not a guarantee of perfect tracking through occlusion; the UI explains skips.

Detection and blending reuse existing detector preprocessing, ArcFace and swap
models. Cancellation is checked at compressed blocks, validation frames,
identity candidates, swap boundaries and encoded chunks. Failed/cancelled
outputs, including trailer failures, are removed. Success returns `result.gif`,
MIME `image/gif` and original canvas dimensions.

The deadline is checked between model calls; it cannot interrupt a native GPU
call already running. It leaves room inside the hosted 150-second job deadline
for input/output transfer. Hardware and contention affect processing time.

Per-frame progress uses the existing heartbeat progress field for GIF tasks.
Lease intervals, fencing, pre-upload ownership confirmation, bounded upload and
terminal cleanup are unchanged. No separate process, queue or extra claim is used.
