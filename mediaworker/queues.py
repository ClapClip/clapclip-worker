"""Route existing queue namespaces without coupling transport to engine work."""
from __future__ import annotations

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
    if phase not in {"info", "download", "download_source", "process", "probe"}:
        phase = "process"
    reason = getattr(error, "diagnostic_code", str(error))
    if not _safe_error_code(reason):
        reason = "UNKNOWN_FAILURE"
    message = str(error)
    if not _safe_error_code(message):
        message = "MEDIA_UNAVAILABLE"
    return FailureReport(message, include_traceback=False, phase=phase, diagnostic_code=reason)


def _safe_error_code(value) -> bool:
    return (isinstance(value, str) and value.isupper()
            and value.replace("_", "").isalpha() and len(value) <= 64)
