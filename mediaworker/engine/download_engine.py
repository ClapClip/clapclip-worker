"""CPU-only yt-dlp engine using the existing outbound worker job protocol."""
from __future__ import annotations

import json
import logging

from .base import Engine, FailureReport, JobContext, RunResult
from ..downloader.egress import EgressProxy
from ..downloader.safety import ServiceError, validate_url
from ..downloader.runtime import check_runtime
from ..downloader.worker import Config as DownloadConfig, ProcessRunner, finite_number, format_options
from ..queues import queue_failure_report

# Public current policy entry points.
from ..downloader.policy import (
    DOWNLOAD_LIMITS, DOWNLOAD_POLICY_VERSIONS,
    METADATA_BYTES, resolve_job_config,
)

log = logging.getLogger(__name__)


class DownloadEngine(Engine):
    task_type = "download"

    def __init__(self, cfg):
        self.config = DownloadConfig.from_env()

    def load(self):
        check_runtime(self.config)
        log.info("download policy ready wire=%s (video<=1280x720, output<=300s, media bytes=unbounded, dns=%s)",
                 list(DOWNLOAD_POLICY_VERSIONS), "cloudflare" if self.config.public_dns else "system")

    def run(self, ctx: JobContext) -> RunResult:
        url, platform = validate_url(ctx.params.get("url"), self.config.extra_hosts)
        operation = ctx.params.get("operation")
        if operation not in {"info", "download"}:
            raise ValueError("invalid download operation")
        try:
            job_config = resolve_job_config(ctx.params.get("limits"), self.config)
        except ServiceError as exc:
            error = RuntimeError(exc.code)
            error.phase = operation
            error.diagnostic_code = exc.code
            raise error from None
        log.info("job %s download bounds (video<=%dx%d, output<=%ds, media bytes=unbounded, dns=%s)",
                 ctx.job_id, job_config.max_width, job_config.max_height,
                 job_config.max_duration,
                 "cloudflare" if job_config.public_dns else "system")
        proxy = EgressProxy(job_config.job_timeout, job_config.public_dns)
        runner = ProcessRunner(job_config, proxy)
        try:
            if operation == "info":
                info = runner.inspect(url, ctx.workdir, ctx.cancel)
                if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"} or info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
                    raise ServiceError("MEDIA_UNAVAILABLE", "Choose one public recorded video.", 422)
                duration = finite_number(info.get("duration"))
                if duration is None or duration <= 0:
                    raise ServiceError("DURATION_LIMIT", "The source duration could not be determined.", 422)
                options = format_options(info, job_config.max_width, job_config.max_height)
                if not options:
                    raise ServiceError("NO_FORMATS", "No supported format is available.", 422)
                data = {"id": ctx.job_id, "title": str(info.get("title") or "Video")[:200],
                        "platform": platform, "duration": duration,
                        "formats": [option["public"] for option in options.values()], "options": options}
                output = ctx.workdir / "result.json"
                output.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                if output.stat().st_size > METADATA_BYTES:
                    raise ServiceError("MEDIA_UNAVAILABLE", "Metadata is too large.", 422)
                return RunResult(output, 1, 1, "application/json")
            option = ctx.params.get("option")
            if not isinstance(option, dict):
                raise ValueError("invalid format option")
            output = runner.download({"cancel": ctx.cancel}, {"url": url}, option, ctx.workdir, ctx.progress)
            content_type = {".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg"}[output.suffix]
            width, height, duration = runner.output_details
            return RunResult(output, width, height, content_type, duration=duration)
        except ServiceError as exc:
            # Only a structured code reaches the public queue, never tool stderr.
            error = RuntimeError(exc.code)
            error.phase = getattr(exc, "phase", operation)
            error.diagnostic_code = getattr(exc, "diagnostic_code", exc.code)
            raise error from None
        finally:
            proxy.close()

    def max_result_bytes(self, params: dict) -> int | None:
        """Return the strict per-job upload bound for the claimed download."""
        try:
            resolve_job_config(params.get("limits"), self.config)
            return METADATA_BYTES if params.get("operation") == "info" else None
        except ServiceError:
            return 0

    def result_byte_limit(self, params: dict, default: int) -> int | None:
        return self.max_result_bytes(params)

    def failure_report(self, error: Exception, params: dict) -> FailureReport:
        return queue_failure_report(self.task_type, error, params)
