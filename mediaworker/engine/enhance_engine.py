from __future__ import annotations

import logging
from dataclasses import dataclass
from math import lcm
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from PIL import Image, ImageOps

from ..config import Config
from ..images import validate_image
from .base import Engine, JobContext, RunResult, session_options
from .enhance_portrait import restore_portraits

log = logging.getLogger(__name__)
GPU_PROVIDERS = {"DmlExecutionProvider", "CUDAExecutionProvider"}


@dataclass(frozen=True)
class ModelSpec:
    filename: str
    native_scale: int
    tile: int
    padding: int
    input_multiple: int = 1


# 与桌面 Enhance 使用相同权重、窗口及 RGB 预处理；图片不使用视频补帧。
MODEL_SPECS = {
    "standard": (ModelSpec("RealESRGAN_x2plus_512_fp32.onnx", 2, 384, 64),),
    "fast": (ModelSpec("span_liveaction_x2_704_fp16.onnx", 2, 640, 32),),
    "deblur": (ModelSpec("nafnet_deblur_512_fp32.onnx", 1, 448, 32),),
    "animation": (
        ModelSpec("realcugan-pro-conservative-up2x.onnx", 2, 256, 16, 4),
        ModelSpec("realcugan-conservative-up4x.onnx", 4, 256, 16, 4),
    ),
}
FACE_FILES = {"gfpgan": "gfpgan_1.4.onnx", "gpen": "gpen_bfr_256.onnx"}
MAX_SCALE = {"standard": 4, "fast": 4, "deblur": 1, "animation": 4, "portrait": 4}


class OnnxModel:
    """启动时加载、复用 GPU 会话；初始化和推理均不允许 CPU 降级。"""

    def __init__(self, path: Path, preferred: list[str]) -> None:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
        candidates = [p for p in preferred if p in GPU_PROVIDERS and p in available]
        failures = []
        self.session = None
        for provider in dict.fromkeys(candidates):
            try:
                if provider == "CUDAExecutionProvider" and hasattr(ort, "preload_dlls"):
                    ort.preload_dlls(directory="")
                options = session_options([provider])
                options.intra_op_num_threads = 2
                options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
                session = ort.InferenceSession(str(path), sess_options=options, providers=[provider])
                session.disable_fallback()
                if provider not in session.get_providers():
                    raise RuntimeError("requested GPU provider was not loaded")
                self.session = session
                self.provider = provider
                break
            except Exception as exc:
                failures.append(f"{provider}: {exc}")
        if self.session is None:
            raise RuntimeError("enhance requires DirectML or CUDA; " + "; ".join(failures))
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        if len(inputs) != 1 or not outputs:
            raise ValueError("enhance requires one RGB model input and an output")
        self.shape = tuple(inputs[0].shape)
        if (len(self.shape) != 4 or self.shape[1] != 3
                or isinstance(self.shape[0], int) and self.shape[0] != 1
                or inputs[0].type not in {"tensor(float)", "tensor(float16)"}):
            raise ValueError("enhance requires an NCHW float RGB model")
        self.dtype = np.float16 if inputs[0].type == "tensor(float16)" else np.float32
        self.input_name, self.output_name = inputs[0].name, outputs[0].name
        metadata = self.session.get_modelmeta().custom_metadata_map
        self.multiple = int(metadata.get("clapclip.input_multiple", "1"))
        if not 1 <= self.multiple <= 64:
            raise ValueError("invalid enhance model alignment")
        self.reference_tiles = metadata.get("clapclip.reference_tiles") == "1"
        log.info("enhance model ready: %s provider=%s", path.name, self.provider)

    def run(self, rgb: np.ndarray, divisor: float = 255, bias: float = 0,
            bgr: bool = False) -> np.ndarray:
        # FP16 与桌面端一致：先舍入输入，再在 float32 归一化后转回 FP16。
        array = (rgb[..., ::-1] if bgr else rgb).astype(self.dtype).astype(np.float32)
        array = np.ascontiguousarray(((array + bias) / divisor).transpose(2, 0, 1)[None], dtype=self.dtype)
        output = np.asarray(self.session.run([self.output_name], {self.input_name: array})[0])
        if not np.issubdtype(output.dtype, np.floating) or not np.isfinite(output).all():
            raise ValueError("enhance model returned non-finite or non-floating output")
        return output.astype(np.float32, copy=False)


def _fixed(model: OnnxModel) -> bool:
    return any(isinstance(value, int) and value > 0 for value in model.shape[2:])


def _canvas_size(width: int, height: int, spec: ModelSpec, model: OnnxModel) -> tuple[int, int]:
    if model.reference_tiles:
        window = spec.tile + 2 * spec.padding
        return max(width, window), max(height, window)
    step = spec.tile if _fixed(model) else lcm(spec.input_multiple, model.multiple)
    return (width + step - 1) // step * step, (height + step - 1) // step * step


def _predict(model: OnnxModel, image: np.ndarray, native_scale: int) -> np.ndarray:
    prediction = model.run(image)
    height, width = image.shape[:2]
    if prediction.shape != (1, 3, height * native_scale, width * native_scale):
        raise ValueError("enhance model returned unexpected dimensions")
    return prediction[0].transpose(1, 2, 0)


def _offsets(length: int, window: int, stride: int) -> list[int]:
    result = [0]
    while result[-1] < length - window:
        result.append(min(result[-1] + stride, length - window))
    return result


def _reference_restore(rgb: np.ndarray, model: OnnxModel, spec: ModelSpec) -> np.ndarray:
    height, width = rgb.shape[:2]
    window, scale = spec.tile + 2 * spec.padding, spec.native_scale
    padded = cv2.copyMakeBorder(rgb, 0, max(0, window - height), 0,
                               max(0, window - width), cv2.BORDER_REFLECT_101)
    rows, cols = padded.shape[:2]
    accumulated = np.zeros((rows * scale, cols * scale, 3), np.float32)
    weights = np.zeros((rows * scale, cols * scale), np.float32)
    side, edge = window * scale, spec.padding * scale
    for y in _offsets(rows, window, spec.tile):
        for x in _offsets(cols, window, spec.tile):
            predicted = _predict(model, padded[y:y + window, x:x + window], scale)
            wy, wx = np.ones(side, np.float32), np.ones(side, np.float32)
            if edge:
                ramp = np.arange(1, side + 1, dtype=np.float32) / edge
                if y > 0:
                    wy = np.minimum(wy, ramp)
                if y + window < rows:
                    wy = np.minimum(wy, ramp[::-1])
                if x > 0:
                    wx = np.minimum(wx, ramp)
                if x + window < cols:
                    wx = np.minimum(wx, ramp[::-1])
            weight = wy[:, None] * wx[None, :]
            region = np.s_[y * scale:y * scale + side, x * scale:x * scale + side]
            accumulated[region] += predicted * weight[..., None]
            weights[region] += weight
    # 与参考路径一致：先量化为 uint8，随后才按用户选择缩回 1×。
    accumulated = accumulated[:height * scale, :width * scale]
    weights = weights[:height * scale, :width * scale]
    return np.clip(accumulated / weights[..., None] * 255, 0, 255).astype(np.uint8)


def restore_image(rgb: np.ndarray, model: OnnxModel, spec: ModelSpec, scale: int) -> np.ndarray:
    height, width = rgb.shape[:2]
    fixed = _fixed(model)
    window = spec.tile + 2 * spec.padding
    if fixed and model.shape[2:] != (window, window):
        raise ValueError("enhance model tile size does not match settings")
    if model.reference_tiles:
        if not fixed:
            raise ValueError("reference tiles require a fixed input")
        restored = _reference_restore(rgb, model, spec)
    else:
        cols, rows = _canvas_size(width, height, spec, model)
        pad = spec.padding if fixed else 0
        padded = cv2.copyMakeBorder(rgb, pad, pad + rows - height, pad,
                                   pad + cols - width, cv2.BORDER_REFLECT_101)
        native = spec.native_scale
        restored = np.empty((rows * native, cols * native, 3), np.uint8)
        for y in range(0, rows, spec.tile):
            for x in range(0, cols, spec.tile):
                right, bottom = min(x + spec.tile, cols), min(y + spec.tile, rows)
                if fixed:
                    x0, y0, x1, y1 = x, y, x + window, y + window
                else:
                    x0, y0 = max(0, x - spec.padding), max(0, y - spec.padding)
                    x1, y1 = min(cols, right + spec.padding), min(rows, bottom + spec.padding)
                    step = lcm(spec.input_multiple, model.multiple)
                    x0, y0 = x0 // step * step, y0 // step * step
                    x1, y1 = min(cols, (x1 + step - 1) // step * step), min(rows, (y1 + step - 1) // step * step)
                predicted = _predict(model, padded[y0:y1, x0:x1], native)
                sx = (spec.padding if fixed else x - x0) * native
                sy = (spec.padding if fixed else y - y0) * native
                core = predicted[sy:sy + (bottom - y) * native, sx:sx + (right - x) * native]
                restored[y * native:bottom * native, x * native:right * native] = np.clip(np.rint(core * 255), 0, 255).astype(np.uint8)
        restored = restored[:height * native, :width * native]
    target = (width * scale, height * scale)
    if restored.shape[:2] == target[::-1]:
        return restored
    return cv2.resize(restored, target, interpolation=cv2.INTER_AREA if scale <= spec.native_scale else cv2.INTER_CUBIC)


def _number(params: dict, name: str, default: float, maximum: float) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"enhance {name} must be in [0, {maximum}]")
    return float(value)


class EnhanceEngine(Engine):
    """单张图片的基础/人像修复，所有模型在启动时加载并复用。"""

    task_type = "enhance"

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._models: dict[str, list[tuple[ModelSpec, OnnxModel]]] = {}
        self._faces: dict[str, OnnxModel] = {}
        self._detector: OnnxModel | None = None
        # 同一引擎只处理一张图，约束中间画布内存，并满足 DirectML Run 串行要求。
        self._run_lock = Lock()

    @property
    def available_models(self) -> list[str]:
        return list(self._models) + (["portrait"] if self._faces else [])

    def load(self) -> None:
        if self._models or self._faces:
            return
        root = self._cfg.enhance_models_dir
        unknown = set(self._cfg.enhance_models) - MODEL_SPECS.keys()
        unknown_faces = set(self._cfg.enhance_face_models) - FACE_FILES.keys()
        if unknown or unknown_faces:
            raise ValueError(f"unsupported enhance model configuration: {sorted(unknown | unknown_faces)}")
        for name in dict.fromkeys(self._cfg.enhance_models):
            specs = MODEL_SPECS.get(name)
            if specs is None:
                raise ValueError(f"unsupported enhance model: {name}")
            try:
                loaded = []
                for spec in specs:
                    path = root / spec.filename
                    if not path.is_file():
                        raise FileNotFoundError(path)
                    model = OnnxModel(path, self._cfg.onnx_providers)
                    if model.reference_tiles and not _fixed(model):
                        raise ValueError("reference tiles require a fixed input")
                    if _fixed(model) and model.shape[2:] != (spec.tile + 2 * spec.padding,) * 2:
                        raise ValueError(f"invalid input size: {spec.filename}")
                    loaded.append((spec, model))
                self._models[name] = loaded
            except Exception:
                log.exception("enhance model unavailable: %s", name)
        face_names = list(dict.fromkeys(self._cfg.enhance_face_models))
        if face_names:
            try:
                self._detector = OnnxModel(root / "yoloface_8n.onnx", self._cfg.onnx_providers)
                if self._detector.shape != (1, 3, 640, 640):
                    raise ValueError("invalid enhance face detector shape")
                for name in face_names:
                    if name not in FACE_FILES:
                        raise ValueError(f"unsupported enhance face model: {name}")
                    try:
                        model = OnnxModel(root / FACE_FILES[name], self._cfg.onnx_providers)
                        size = 512 if name == "gfpgan" else 256
                        if model.shape != (1, 3, size, size):
                            raise ValueError("invalid enhance portrait model shape")
                        self._faces[name] = model
                    except Exception:
                        log.exception("enhance portrait model unavailable: %s", name)
            except Exception:
                self._detector = None
                log.exception("enhance face detector unavailable")
        if not self.available_models:
            raise RuntimeError("no enhance GPU models loaded; check ENHANCE_MODELS_DIR")

    def run(self, ctx: JobContext) -> RunResult:
        if not self.available_models:
            raise RuntimeError("enhance engine is not loaded")
        if ctx.task_type != self.task_type:
            raise ValueError("enhance accepts image enhancement tasks only")
        if set(ctx.inputs) != {"image"}:
            raise ValueError("enhance job requires exactly one image input")
        params = ctx.params
        if set(params) - {"model", "scale", "face", "faceStrength", "faceEdgeBlend", "faceFusionArea"}:
            raise ValueError("unsupported enhance image parameters")
        name, scale, face = params.get("model", "standard"), params.get("scale", 1), params.get("face", "off")
        if not isinstance(name, str) or name not in self.available_models:
            raise ValueError("unsupported or unavailable enhance model")
        if type(scale) is not int or scale not in (1, 2, 4) or scale > MAX_SCALE[name]:
            raise ValueError("unsupported enhance scale for this model")
        if not isinstance(face, str) or face not in {"off", *FACE_FILES}:
            raise ValueError("unsupported enhance face model")
        if face != "off" and (face not in self._faces or self._detector is None):
            raise ValueError("enhance portrait model is unavailable")
        strength = _number(params, "faceStrength", 1, 1)
        edge = _number(params, "faceEdgeBlend", .4, 1)
        area = params.get("faceFusionArea", 50)
        if type(area) is not int or not 0 <= area <= 50:
            raise ValueError("enhance faceFusionArea must be an integer in [0, 50]")
        if name == "portrait" and (face == "off" or strength == 0):
            raise ValueError("portrait enhancement requires an active face model")
        source = ctx.inputs["image"]
        width, height = validate_image(source, self._cfg)
        if width * height * scale * scale > self._cfg.max_output_pixels:
            raise ValueError("enhance output exceeds pixel limit")
        with self._run_lock:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGBA")
                image.load()
            rgb = np.asarray(image.convert("RGB"))
            if name == "portrait":
                enhanced = cv2.resize(rgb, (image.width * scale, image.height * scale), interpolation=cv2.INTER_CUBIC)
            else:
                spec, model = self._models[name][-1 if name == "animation" and scale == 4 else 0]
                # A native 2x model reaches 4x with two AI passes rather than
                # stretching the first result with bicubic interpolation.
                passes = 2 if scale == 4 and spec.native_scale == 2 else 1
                pass_scale = 2 if passes == 2 else scale
                enhanced = rgb
                for _ in range(passes):
                    rows_in, cols_in = enhanced.shape[:2]
                    cols, rows = _canvas_size(cols_in, rows_in, spec, model)
                    if cols * rows * spec.native_scale ** 2 > self._cfg.max_output_pixels:
                        raise ValueError("enhance intermediate image exceeds pixel limit")
                    enhanced = restore_image(enhanced, model, spec, pass_scale)
            if face != "off" and strength > 0:
                enhanced = restore_portraits(rgb, enhanced, self._detector, self._faces[face],
                                             strength=strength, edge_blend=edge, fusion_area=area)
            result = Image.fromarray(enhanced)
            # 调整透明通道到最终尺寸，包含调色板 PNG 的透明信息。
            alpha = image.getchannel("A")
            if alpha.getextrema() != (255, 255):
                result.putalpha(alpha.resize(result.size, Image.Resampling.LANCZOS))
            destination = Path(ctx.workdir) / "result.png"
            result.save(destination, "PNG", compress_level=6)
        log.info("enhance complete (%dx%d -> %dx%d, model=%s, face=%s)", image.width, image.height, result.width, result.height, name, face)
        return RunResult(destination, result.width, result.height, "image/png")
