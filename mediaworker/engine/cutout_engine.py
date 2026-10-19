from __future__ import annotations

import logging
from contextlib import nullcontext
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageOps

from ..config import Config
from ..images import validate_image
from .base import (
    Engine,
    JobContext,
    RunResult,
    resolve_providers,
    session_options,
)

log = logging.getLogger(__name__)

MODEL_FILES = {"isnet-general-use": "isnet-general-use.onnx"}
MODEL_SIZE = (1024, 1024)


class CutoutEngine(Engine):
    """Remove an image background with one reusable local ONNX session."""

    task_type = "cutout"

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._session: Any | None = None
        self._run_lock = Lock()
        self._serialize_runs = True

    @property
    def available_models(self) -> list[str]:
        return [self._cfg.cutout_model] if self._session is not None else []

    def load(self) -> None:
        filename = MODEL_FILES.get(self._cfg.cutout_model)
        if filename is None:
            raise ValueError(f"unsupported cutout model: {self._cfg.cutout_model}")
        model_path = self._cfg.cutout_models_dir / filename
        if not model_path.is_file():
            raise FileNotFoundError(
                f"cutout model not found at {model_path}; restart with model sync enabled"
            )
        import onnxruntime as ort

        providers = resolve_providers(self._cfg.onnx_providers)
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=session_options(providers),
            providers=providers,
        )
        self._serialize_runs = "DmlExecutionProvider" in self._session.get_providers()
        log.info(
            "cutout model ready: %s providers=%s",
            self._cfg.cutout_model,
            self._session.get_providers(),
        )

    def _mask(self, image: Image.Image) -> Image.Image:
        array = np.asarray(
            image.convert("RGB").resize(MODEL_SIZE, Image.Resampling.LANCZOS),
            dtype=np.float32,
        )
        array /= max(float(array.max()), 1e-6)
        array = (array - 0.5).transpose(2, 0, 1)[None, ...]
        input_name = self._session.get_inputs()[0].name
        # DirectML permits only one Run call at a time per shared session.
        # Keep preprocessing, exports, and other engines independently usable.
        with self._run_lock if self._serialize_runs else nullcontext():
            outputs = self._session.run(None, {input_name: array})
        if not outputs:
            raise ValueError("cutout model returned no mask")
        prediction = np.asarray(outputs[0])
        if (
            prediction.shape != (1, 1, *MODEL_SIZE)
            or not np.isfinite(prediction).all()
        ):
            raise ValueError("cutout model returned an invalid mask")
        prediction = prediction[0, 0]
        low, high = float(prediction.min()), float(prediction.max())
        if high - low <= 1e-6:
            # A uniform foreground prediction is opaque, not an empty image.
            normalized = np.clip(prediction * 255, 0, 255).astype(np.uint8)
        else:
            normalized = (prediction - low) / (high - low)
            normalized = np.clip(normalized * 255, 0, 255).astype(np.uint8)
        return Image.fromarray(normalized).resize(
            image.size, Image.Resampling.LANCZOS
        )

    def run(self, ctx: JobContext) -> RunResult:
        if self._session is None:
            raise RuntimeError("cutout engine is not loaded")
        source = ctx.inputs.get("image")
        if source is None:
            raise ValueError("cutout job requires an image input")

        validate_image(source, self._cfg)
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            image.load()
        # Never bring back pixels that were already transparent in the input.
        image.putalpha(ImageChops.multiply(image.getchannel("A"), self._mask(image)))
        destination = Path(ctx.workdir) / "result.png"
        image.save(destination, "PNG", compress_level=6)
        width, height = image.size
        log.info("cutout complete (%dx%d)", width, height)
        return RunResult(destination, width, height, "image/png")
