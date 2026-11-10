"""Route existing queue namespaces without coupling transport to engine work."""
from __future__ import annotations

import json

import httpx

from .models import ClaimedJob
from .engine.base import FailureReport


class QueueRoutes:
    def __init__(self, download_prefix: str) -> None:
        if not download_prefix.startswith("/api/worker/") or ".." in download_prefix.split("/"):
            raise ValueError("WORKER_DOWNLOAD_API_PREFIX must be a safe /api/worker/... path")
        self.download_prefix = download_prefix
        self.job_routes: dict[str, str] = {}

    def claim_path(self, capabilities: list[str]) -> str:
        prefix = self.download_prefix if capabilities and set(capabilities) == {"download"} else "/api/worker"
        return f"{prefix}/claim"

    def bind(self, job: ClaimedJob) -> None:
        # A download returned by the shared AI route still ACKs in its own queue.
        if job.task_type == "download":
            self.job_routes[job.id] = self.download_prefix

    def job_path(self, job_id: str, operation: str) -> str:
        return f"{self.job_routes.get(job_id, '/api/worker')}/{operation}"


def lease_heartbeat_interval(task_type: str, lease_seconds: float) -> float:
    """Download progress needs frequent beats; both queues renew before expiry."""
    maximum = 3.0 if task_type == "download" else 30.0
    return max(0.25, min(maximum, lease_seconds / 3.0))


def queue_failure_report(task_type: str, error: Exception, params: dict) -> FailureReport:
    """Preserve each queue's failure wire format, even when no engine is loaded."""
    if task_type != "download":
        return FailureReport(str(error))
    # Extractor/S3 exceptions can contain credentials or signed URLs. Only
    # reviewed categories reach the public download queue or diagnostics.
    phase = getattr(error, "phase", params.get("operation", "download"))
    if phase not in {"info", "download", "download_source", "process", "probe", "transcode", "output_probe"}:
        phase = "process"
    from .downloader.safety import ServiceError
    # URL admission happens before a proxy/runner exists. Preserve only the
    # reviewed public codes, rather than interpreting its human message.
    admission_code = (error.code if isinstance(error, ServiceError) and isinstance(error.code, str)
                      and error.code in {"INVALID_URL", "UNSUPPORTED_SITE"} else None)
    reason = admission_code or getattr(error, "diagnostic_code", str(error))
    if not _safe_error_code(reason):
        reason = _safe_failure_kind(error)
    message = admission_code or str(error)
    if not _safe_error_code(message):
        message = "MEDIA_UNAVAILABLE"
    return FailureReport(message, include_traceback=False, phase=phase, diagnostic_code=reason)


def _safe_error_code(value) -> bool:
    return (isinstance(value, str) and value.isupper()
            and value.replace("_", "").isalpha() and len(value) <= 64)


def _safe_failure_kind(error):
    # Fixed categories only: exception messages and arbitrary class names can
    # contain source URLs, signatures or extractor output.
    for kind, code in ((json.JSONDecodeError, "INVALID_SOURCE_JSON"),
                       (httpx.TimeoutException, "NETWORK_TIMEOUT"),
                       (httpx.RemoteProtocolError, "NETWORK_PROTOCOL_ERROR"),
                       (httpx.NetworkError, "NETWORK_ERROR"),
                       (TimeoutError, "OPERATION_TIMEOUT"),
                       (OSError, "LOCAL_IO_ERROR"),
                       (ValueError, "INVALID_DATA"),
                       (TypeError, "INVALID_DATA"),
                       (AttributeError, "INVALID_DATA"),
                       (RuntimeError, "RUNTIME_ERROR")):
        if isinstance(error, kind):
            return code
    return "UNKNOWN_FAILURE"
