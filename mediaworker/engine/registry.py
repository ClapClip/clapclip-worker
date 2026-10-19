from __future__ import annotations

import logging
import os
from collections.abc import Callable

from ..config import Config
from .base import Engine

log = logging.getLogger(__name__)

EngineFactory = Callable[[Config], Engine]


def _faceswap(cfg: Config) -> Engine:
    from .faceswap_engine import FaceSwapEngine

    return FaceSwapEngine(cfg)


def _cutout(cfg: Config) -> Engine:
    from .cutout_engine import CutoutEngine

    return CutoutEngine(cfg)


def _enhance(cfg: Config) -> Engine:
    from .enhance_engine import EnhanceEngine

    return EnhanceEngine(cfg)


def _download(cfg: Config) -> Engine:
    from .download_engine import DownloadEngine

    return DownloadEngine(cfg)


# Task imports stay lazy: a cutout-only node does not import InsightFace, and a
# face-swap-only node does not import the cutout engine. Adding a task is one factory
# entry instead of another branch in registry construction.
ENGINE_FACTORIES: dict[str, EngineFactory] = {
    "faceswap": _faceswap,
    "cutout": _cutout,
    "enhance": _enhance,
    "download": _download,
}


def _mock_enabled() -> bool:
    return os.environ.get("MEDIAWORKER_MOCK", "").lower() in ("1", "true", "yes")


def build_registry(cfg: Config, capabilities: list[str]) -> dict[str, Engine]:
    """Instantiate and warm the engines this node is configured for. An engine
    that fails to load is skipped (logged) so one bad model doesn't take the
    whole node down — the node simply won't advertise that capability."""
    registry: dict[str, Engine] = {}
    for cap in capabilities:
        factory = ENGINE_FACTORIES.get(cap)
        if factory is None:
            log.warning("no engine registered for capability %r; skipping", cap)
            continue
        try:
            if _mock_enabled():
                # Exercise the complete queue protocol without loading models.
                # Remote APIs reject mock nodes in runner.run_forever().
                from .mock_engine import MockEngine

                engine = MockEngine(cfg, cap)
                log.warning("MEDIAWORKER_MOCK on: %s runs the echo mock engine", cap)
            else:
                engine = factory(cfg)
            engine.load()
            registry[cap] = engine
            log.info("capability ready: %s", cap)
        except Exception:
            log.exception("failed to load engine for %r; skipping", cap)
    return registry
