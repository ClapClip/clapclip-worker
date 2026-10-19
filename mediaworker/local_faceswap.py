"""Full-photo adapter for the standalone UI; queue crop contract stays unchanged."""
from dataclasses import replace
import threading

import cv2
import numpy as np
from PIL import Image, ImageOps

from .engine.base import Engine, RunResult
from .engine.faceswap_engine import FaceSwapEngine, SWAP_MODELS, _box_mask, _paste_face
from .engine.enhance_portrait import _face_indices
from .images import validate_image


def similarity(points, target):
    design = np.zeros((10, 4), dtype=np.float64)
    design[0::2, 0], design[0::2, 1], design[0::2, 2] = points[:, 0], -points[:, 1], 1
    design[1::2, 0], design[1::2, 1], design[1::2, 3] = points[:, 1], points[:, 0], 1
    fit, _, rank, _ = np.linalg.lstsq(design, target.reshape(10), rcond=None)
    if rank != 4 or not np.isfinite(fit).all() or np.linalg.norm(design @ fit - target.reshape(10)) > 0.25 * target.max():
        raise ValueError("无法对齐人脸，请换一张清晰照片")
    a, b, x, y = fit
    return np.array([[a, -b, x], [b, a, y]], dtype=np.float32)


class LocalFaceSwap(Engine):
    task_type = "faceswap"

    def __init__(self, cfg):
        self.cfg, self.engine = cfg, FaceSwapEngine(cfg)
        self.lock = threading.Lock()

    def load(self):
        import onnxruntime as ort
        from .engine.base import resolve_providers, session_options
        providers = resolve_providers(self.cfg.onnx_providers)
        self.detector = ort.InferenceSession(str(self.cfg.enhance_models_dir / "yoloface_8n.onnx"), sess_options=session_options(providers), providers=providers)
        self.input_name = self.detector.get_inputs()[0].name
        self.dtype = np.float16 if self.detector.get_inputs()[0].type == "tensor(float16)" else np.float32
        self.engine.load()

    @property
    def available_models(self):
        return self.engine.available_models

    def align(self, image, size):
        from insightface.utils.face_align import arcface_dst
        height, width = image.shape[:2]
        factor = min(1, 640 / max(width, height))
        dw, dh = max(1, int(width * factor)), max(1, int(height * factor))
        padded = np.zeros((640, 640, 3), dtype=np.uint8)
        padded[:dh, :dw] = cv2.resize(image[..., ::-1], (dw, dh))
        array = ((padded[..., ::-1].astype(np.float32) - 127.5) / 128).transpose(2, 0, 1)[None]
        output = np.asarray(self.detector.run(None, {self.input_name: np.ascontiguousarray(array, dtype=self.dtype)})[0])
        if output.ndim != 3 or output.shape[1] != 20 or not np.isfinite(output).all():
            raise ValueError("人脸检测输出无效")
        rows = output[0]
        indices = sorted(_face_indices(rows), key=lambda i: rows[2, i] * rows[3, i], reverse=True)
        target = arcface_dst.copy()
        target[:, 0] += 8
        target *= size / 128
        for i in indices:
            if min(rows[2, i] * width / dw, rows[3, i] * height / dh) < 24:
                continue
            landmarks = rows[5:, i].reshape(5, 3)
            points = landmarks[:, :2] * [width / dw, height / dh]
            if (landmarks[:, 2] < .5).any() or (points < 0).any() or (points >= [width, height]).any() or np.linalg.norm(points[1] - points[0]) < 4:
                continue
            try:
                matrix = similarity(points, target)
            except ValueError:
                continue
            return cv2.warpAffine(image, matrix, (size, size)), matrix
        raise ValueError("未检测到清晰人脸，请换一张照片")

    def run(self, ctx):
        with self.lock:
            model = ctx.params.get("model", "hyperswap_256_b")
            if model not in self.available_models:
                raise ValueError("所选换脸模型尚未安装")
            size = SWAP_MODELS[model].size
            images, crops, matrix = {}, {}, None
            for name in ("target", "source"):
                validate_image(ctx.inputs[name], self.cfg)
                with Image.open(ctx.inputs[name]) as opened:
                    images[name] = np.asarray(ImageOps.exif_transpose(opened).convert("RGB"))[..., ::-1].copy()
                crop, affine = self.align(images[name], size)
                crops[name] = ctx.workdir / (name + "-aligned.png")
                if not cv2.imwrite(str(crops[name]), crop):
                    raise RuntimeError("无法保存人脸裁剪")
                if name == "target":
                    matrix = affine
            if ctx.cancel.is_set():
                raise RuntimeError("已取消")
            result = self.engine.run(replace(ctx, inputs=crops, params={**ctx.params, "model": model, "mode": "face"}))
            rgba = cv2.imread(str(result.output_path), cv2.IMREAD_UNCHANGED)
            mask = _box_mask(size, float(ctx.params.get("edgeBlur", .4)), int(ctx.params.get("fusionArea", 50)))
            if rgba.shape[2] == 4:
                mask *= rgba[..., 3] / 255
            merged = _paste_face(images["target"], rgba[..., :3], mask, matrix)
            output = ctx.workdir / "full-result.png"
            if not cv2.imwrite(str(output), merged):
                raise RuntimeError("无法保存结果")
            return RunResult(output, merged.shape[1], merged.shape[0], "image/png")
