from __future__ import annotations

from pathlib import Path
import warnings

from PIL import Image

from .config import Config


def validate_image(path: Path, cfg: Config) -> tuple[int, int]:
    """Validate headers and structure before any full raster allocation."""
    if not 0 < path.stat().st_size <= cfg.max_image_bytes:
        raise ValueError("input image exceeds byte limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("unsupported image format")
            width, height = image.size
            if width < 1 or height < 1 or max(width, height) > cfg.max_image_dimension or width * height > cfg.max_image_pixels:
                raise ValueError("input image exceeds pixel limit")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("animated input is not supported")
            image.verify()
    return width, height
