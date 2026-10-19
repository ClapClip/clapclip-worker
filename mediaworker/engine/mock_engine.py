from __future__ import annotations

import logging
import shutil

from ..config import Config
from .base import Engine, JobContext, RunResult

log = logging.getLogger(__name__)


class MockEngine(Engine):
    """No-model, no-GPU stand-in for image engines: echoes their input
    image as the result. Lets a node exercise the full claim -> download ->
    process -> upload -> complete loop on a machine without the model/GPU (or
    without the heavy insightface/onnxruntime deps installed). Turn on with
    MEDIAWORKER_MOCK=1 — never in production."""

    @property
    def available_models(self) -> list[str]:
        if self.task_type == "faceswap":
            return ["inswapper_128"]
        if self.task_type == "enhance":
            return list(dict.fromkeys([
                *self._cfg.enhance_models,
                *(["portrait"] if self._cfg.enhance_face_models else []),
            ]))
        return []

    def __init__(self, cfg: Config, task_type: str = "faceswap") -> None:
        self._cfg = cfg
        self.task_type = task_type

    def run(self, ctx: JobContext) -> RunResult:
        preferred = "image" if self.task_type in ("cutout", "enhance") else "target"
        src = ctx.inputs.get(preferred) or (
            next(iter(ctx.inputs.values())) if ctx.inputs else None
        )
        if src is None:
            raise ValueError("mock engine requires at least one input")

        use_png = self.task_type in ("cutout", "enhance") or ctx.params.get("mode") == "face"
        out = ctx.workdir / ("result.png" if use_png else "result.jpg")
        width, height = 512, 512
        try:
            from PIL import Image

            with Image.open(src) as img:
                rendered = img.convert("RGBA" if use_png else "RGB")
                width, height = rendered.size
                rendered.save(out, "PNG" if use_png else "JPEG", quality=92)
        except Exception:
            # PIL missing or undecodable input: copy the bytes through as-is.
            shutil.copyfile(src, out)
        log.info("mock %s engine echoed input (%dx%d)", self.task_type, width, height)
        return RunResult(
            output_path=out,
            width=width,
            height=height,
            content_type="image/png" if use_png else "image/jpeg",
        )
