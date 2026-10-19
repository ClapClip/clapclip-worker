from __future__ import annotations

import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


@dataclass
class JobContext:
    """Everything an engine needs to run one job, with inputs already fetched to
    local files. Queue authentication and S3 transport stay in the lifecycle;
    media engines may own their controlled public-source transport."""

    job_id: str
    task_type: str
    params: dict
    inputs: dict[str, Path]  # name -> local file path
    workdir: Path
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Callable[[str, int], None] = field(default=lambda _status, _percent: None)


@dataclass
class RunResult:
    output_path: Path
    width: int
    height: int
    content_type: str = "image/jpeg"
    duration: float | None = None


@dataclass(frozen=True)
class FailureReport:
    """Public failure message and the engine's safe diagnostic representation."""

    message: str
    include_traceback: bool = True
    phase: str = "process"
    diagnostic_code: str = "UNKNOWN_FAILURE"


class Engine(ABC):
    """A pluggable processor for one `task_type`. Mirrors the frontend's
    swappable FaceSwapProvider idea, on the compute side.

    `load()` runs once at startup and must warm every model into memory —
    never load per job, that's the single biggest performance mistake. `run()`
    is called (off the event loop, in a worker thread) for each job."""

    task_type: str = ""

    @property
    def available_models(self) -> list[str]:
        return []

    def load(self) -> None:  # noqa: B027 - optional hook
        """Warm models. Override; default is a no-op for trivial engines."""

    def result_byte_limit(self, params: dict, default: int) -> int | None:
        """Default image bound; a media engine may apply its per-job contract."""
        return default

    def failure_report(self, error: Exception, params: dict) -> FailureReport:
        """Preserve image failures; source-facing engines redact tool details."""
        return FailureReport(str(error))

    @abstractmethod
    def run(self, ctx: JobContext) -> RunResult:
        ...


def session_options(
    providers: list[str], *, disable_graph_optimizations: bool = False
):
    """Build options compatible with the selected execution providers."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    if disable_graph_optimizations:
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    if "DmlExecutionProvider" in providers:
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return options


def resolve_providers(preferred: list[str]) -> list[str]:
    """Keep only the ONNX Runtime execution providers actually installed, in
    the caller's priority order, always ending with CPU. Lets one build run on
    a CoreML Mac, a CUDA/DirectML Windows box, or a plain CPU machine without
    per-node code changes."""
    try:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
    except Exception as exc:  # pragma: no cover
        log.warning("onnxruntime not importable (%s); assuming CPU only", exc)
        available = {"CPUExecutionProvider"}

    chosen = [p for p in preferred if p in available]
    if sys.platform == "win32" and "CUDAExecutionProvider" in chosen:
        # CUDA/cuDNN wheels live outside Windows' default DLL search path.
        ort.preload_dlls(directory="")
    if "CPUExecutionProvider" not in chosen:
        chosen.append("CPUExecutionProvider")
    log.info("onnx providers: requested=%s available=%s -> using=%s",
             preferred, sorted(available), chosen)
    return chosen
