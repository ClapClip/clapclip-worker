from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..config import Config
from ..images import validate_image
from .base import Engine, JobContext, RunResult, resolve_providers, session_options

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwapModel:
    filename: str
    kind: str
    size: int


SWAP_MODELS: dict[str, SwapModel] = {
    "inswapper_128": SwapModel("inswapper_128.onnx", "inswapper", 128),
    "hyperswap_256_a": SwapModel("hyperswap_1a_256.onnx", "hyperswap", 256),
    "hyperswap_256_b": SwapModel("hyperswap_1b_256.onnx", "hyperswap", 256),
    "hyperswap_256_c": SwapModel("hyperswap_1c_256.onnx", "hyperswap", 256),
}
IDENTITY_MODEL = "arcface_w600k_r50.onnx"
FACE_SWAP_MODE = "face"


def _swap_model_paths(cfg: Config) -> dict[str, Path]:
    legacy = os.environ.get("FACESWAP_SWAP_MODEL", "").strip()
    paths: dict[str, Path] = {}
    for key, spec in SWAP_MODELS.items():
        path = cfg.faceswap_models_dir / spec.filename
        if key == "inswapper_128" and legacy:
            legacy_path = Path(os.path.expanduser(legacy))
            path = (
                legacy_path
                if legacy_path.is_absolute()
                else cfg.faceswap_models_dir / legacy_path
            )
        elif key == "inswapper_128" and not path.is_file():
            fp16_path = cfg.faceswap_models_dir / "inswapper_128_fp16.onnx"
            if fp16_path.is_file():
                path = fp16_path
        paths[key] = path
    return paths


def _clip_setting(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(high, max(low, parsed))


def _odd_kernel(sigma: float, size: int) -> tuple[int, int] | None:
    if sigma <= 0:
        return None
    kernel = max(3, int(sigma * 6) | 1)
    kernel = min(kernel, size - 1 if size % 2 == 0 else size)
    if kernel < 3:
        return None
    return kernel, kernel


def _blur_mask(mask: np.ndarray, edge_blur: float) -> np.ndarray:
    sigma = edge_blur * max(mask.shape[:2]) * 0.055
    kernel = _odd_kernel(sigma, min(mask.shape[:2]))
    if kernel:
        mask = cv2.GaussianBlur(mask, kernel, sigmaX=sigma)
    return np.clip(mask, 0, 1)


def _box_mask(size: int, edge_blur: float, fusion_area: int) -> np.ndarray:
    blur_amount = int(size * 0.5 * np.float32(np.clip(edge_blur, 0, 1)))
    padding = 50 - np.clip(fusion_area, 0, 50)
    border = max(blur_amount // 2, 1, int(size * padding / 100))
    mask = np.zeros((size, size), dtype=np.float32)
    mask[border:size - border, border:size - border] = 1
    if blur_amount:
        mask = cv2.GaussianBlur(mask, (0, 0), blur_amount * 0.25)
    return mask


def _resize_model_mask(
    mask: np.ndarray,
    edge_blur: float,
    fusion_area: int,
) -> np.ndarray:
    size = mask.shape[0]
    scale = 0.8 + 0.4 * (fusion_area / 50.0)
    center = (size / 2.0, size / 2.0)
    matrix = cv2.getRotationMatrix2D(center, 0, scale)
    adjusted = cv2.warpAffine(mask, matrix, (size, size), borderValue=0)
    adjusted = np.clip(adjusted, 0, 1)
    return _blur_mask(adjusted, edge_blur)


def _paste_face(
    target: np.ndarray,
    fake: np.ndarray,
    mask: np.ndarray,
    matrix: np.ndarray,
) -> np.ndarray:
    fake_wide = _warp_face(target, fake, matrix)
    height, width = target.shape[:2]
    inverse = cv2.invertAffineTransform(matrix)
    mask_wide = cv2.warpAffine(mask, inverse, (width, height), borderValue=0)
    mask_wide = np.clip(mask_wide, 0, 1)[..., None]
    merged = fake_wide.astype(np.float32) * mask_wide + target.astype(
        np.float32
    ) * (1 - mask_wide)
    return merged.astype(np.uint8)


def _warp_face(
    target: np.ndarray,
    fake: np.ndarray,
    matrix: np.ndarray,
) -> np.ndarray:
    height, width = target.shape[:2]
    inverse = cv2.invertAffineTransform(matrix)
    return cv2.warpAffine(fake, inverse, (width, height), borderValue=0)


def _warp_face_with_alpha(
    target: np.ndarray,
    fake: np.ndarray,
    mask: np.ndarray,
    matrix: np.ndarray,
) -> np.ndarray:
    height, width = target.shape[:2]
    inverse = cv2.invertAffineTransform(matrix)
    fake_wide = cv2.warpAffine(fake, inverse, (width, height), borderValue=0)
    mask_wide = cv2.warpAffine(mask, inverse, (width, height), borderValue=0)
    rgba = cv2.cvtColor(fake_wide, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = np.clip(mask_wide * 255, 0, 255).astype(np.uint8)
    return rgba


class FaceSwapEngine(Engine):
    """Swap browser-aligned face crops with FaceFusion-compatible ONNX models."""

    task_type = "faceswap"

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._recognition = None
        self._models: dict[str, Any] = {}
        self._model_paths = _swap_model_paths(cfg)
        self._model_lock = threading.Lock()

    def load(self) -> None:
        installed = [key for key, path in self._model_paths.items() if path.is_file()]
        if not installed:
            raise FileNotFoundError("no swap models installed; set FACESWAP_MODELS_DIR before starting")
        identity_path = self._cfg.faceswap_models_dir / IDENTITY_MODEL
        if not identity_path.is_file():
            raise FileNotFoundError(
                f"identity model not found at {identity_path}; place {IDENTITY_MODEL} "
                "in FACESWAP_MODELS_DIR"
            )

        import insightface

        providers = resolve_providers(self._cfg.onnx_providers)
        self._recognition = insightface.model_zoo.get_model(
            str(identity_path),
            providers=providers,
            sess_options=session_options(providers),
        )
        if self._recognition is None or getattr(
            self._recognition, "taskname", None
        ) != "recognition":
            raise ValueError(f"unrecognized identity model: {identity_path.name}")
        self._recognition.prepare(ctx_id=0)
        identity_session = getattr(self._recognition, "session", self._recognition)
        log.info(
            "identity model ready: %s providers=%s",
            identity_path.name,
            identity_session.get_providers(),
        )
        for key in installed:
            try:
                self._get_model(key)
            except Exception:
                log.exception("swap model unavailable: %s", key)
        if not self._models:
            raise RuntimeError("no swap models could be loaded")

    @property
    def available_models(self) -> list[str]:
        return list(self._models)

    def _get_model(self, model_key: str) -> Any:
        spec = SWAP_MODELS.get(model_key)
        if not spec:
            raise ValueError(f"unsupported fusion model: {model_key}")
        with self._model_lock:
            if model_key in self._models:
                return self._models[model_key]
            path = self._model_paths[model_key]
            if not path.exists():
                raise FileNotFoundError(
                    f"fusion model not found at {path}; place it in "
                    "FACESWAP_MODELS_DIR or MODELS_DIR"
                )
            providers = resolve_providers(self._cfg.onnx_providers)
            if spec.kind == "inswapper":
                import insightface

                model = insightface.model_zoo.get_model(
                    str(path),
                    providers=providers,
                    sess_options=session_options(providers),
                )
                if model is None:
                    raise ValueError(f"unrecognized swap model: {path.name}")
            else:
                import onnxruntime as ort

                # ORT DirectML 1.22 corrupts HyperSwap's FP16 graph when graph
                # fusion is enabled, producing displaced eyes and mouths. The
                # unoptimized graph stays on DirectML and matches CPU output.
                disable_dml_fusion = "DmlExecutionProvider" in providers
                model = ort.InferenceSession(
                    str(path),
                    providers=providers,
                    sess_options=session_options(
                        providers,
                        disable_graph_optimizations=disable_dml_fusion,
                    ),
                )
            self._models[model_key] = model
            session = getattr(model, "session", model)
            log.info("fusion model ready: %s providers=%s", model_key, session.get_providers())
            return model

    def _swap_inswapper(
        self,
        image: np.ndarray,
        target_face: Any,
        source_face: Any,
        edge_blur: float,
        fusion_area: int,
        paste_back: bool,
    ) -> np.ndarray:
        model = self._get_model("inswapper_128")
        fake, matrix = model.get(
            image,
            target_face,
            source_face,
            paste_back=False,
        )
        if not paste_back:
            # Preserve the complete crop; the browser owns final feathering.
            base_mask = np.ones(fake.shape[:2], dtype=np.float32)
            return _warp_face_with_alpha(image, fake, base_mask, matrix)
        mask = _box_mask(fake.shape[0], edge_blur, fusion_area)
        return _paste_face(image, fake, mask, matrix)

    def _swap_hyperswap(
        self,
        model_key: str,
        image: np.ndarray,
        target_face: Any,
        source_face: Any,
        edge_blur: float,
        fusion_area: int,
        paste_back: bool,
    ) -> np.ndarray:
        from insightface.utils import face_align

        spec = SWAP_MODELS[model_key]
        model = self._get_model(model_key)
        crop, matrix = face_align.norm_crop2(
            image,
            target_face.kps,
            spec.size,
        )
        rgb = crop[:, :, ::-1].astype(np.float32) / 255.0
        rgb = (rgb - 0.5) / 0.5
        target = np.expand_dims(rgb.transpose(2, 0, 1), axis=0)
        source = source_face.normed_embedding.reshape(1, -1).astype(np.float32)
        outputs = model.run(None, {"source": source, "target": target})
        fake = np.clip(outputs[0][0].transpose(1, 2, 0), -1, 1)
        fake = ((fake + 1) * 127.5)[:, :, ::-1].astype(np.uint8)
        model_mask = np.clip(outputs[1][0, 0], 0, 1).astype(np.float32)
        if not paste_back:
            base_mask = _resize_model_mask(model_mask, 0, 50)
            return _warp_face_with_alpha(image, fake, base_mask, matrix)
        mask = _resize_model_mask(model_mask, edge_blur, fusion_area)
        return _paste_face(image, fake, mask, matrix)

    def run(self, ctx: JobContext) -> RunResult:
        target_path = ctx.inputs.get("target")
        source_path = ctx.inputs.get("source")
        if not target_path or not source_path:
            raise ValueError("faceswap requires 'target' and 'source' inputs")

        validate_image(target_path, self._cfg)
        validate_image(source_path, self._cfg)
        target = cv2.imread(str(target_path))
        source = cv2.imread(str(source_path))
        if target is None or source is None:
            raise ValueError("failed to decode input image(s)")

        model_key = str(ctx.params.get("model") or "inswapper_128")
        spec = SWAP_MODELS.get(model_key)
        if not spec:
            raise ValueError(f"unsupported fusion model: {model_key}")
        mode = str(ctx.params.get("mode") or FACE_SWAP_MODE)
        if mode != FACE_SWAP_MODE:
            raise ValueError(
                "faceswap requires browser-aligned face crops (mode='face')"
            )

        # The browser already detects and aligns both crops. Re-running a
        # detector here can reject a valid small crop or move its landmarks.
        from insightface.app.common import Face
        from insightface.utils.face_align import arcface_dst

        expected_shape = (spec.size, spec.size)
        if target.shape[:2] != expected_shape or source.shape[:2] != expected_shape:
            raise ValueError(f"aligned face inputs must both be {spec.size}x{spec.size}")
        if self._recognition is None:
            raise RuntimeError("identity model is not loaded")
        points = arcface_dst.copy()
        points[:, 0] += 8
        points *= spec.size / 128
        target_face = Face(
            kps=points.copy(),
            bbox=np.array([0, 0, spec.size, spec.size]),
        )
        source_face = Face(kps=points.copy())
        self._recognition.get(source, source_face)
        edge_blur = _clip_setting(ctx.params.get("edgeBlur"), 0, 1, 0.4)
        fusion_area = int(
            round(_clip_setting(ctx.params.get("fusionArea"), 0, 50, 50))
        )

        result = target.copy()
        if spec.kind == "inswapper":
            result = self._swap_inswapper(
                result,
                target_face,
                source_face,
                edge_blur,
                fusion_area,
                False,
            )
        else:
            result = self._swap_hyperswap(
                model_key,
                result,
                target_face,
                source_face,
                edge_blur,
                fusion_area,
                False,
            )

        out = ctx.workdir / "result.png"
        if not cv2.imwrite(str(out), result):
            raise RuntimeError("failed to encode result image")
        height, width = result.shape[:2]
        return RunResult(
            output_path=out,
            width=width,
            height=height,
            content_type="image/png",
        )
