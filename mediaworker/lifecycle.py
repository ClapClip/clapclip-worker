"""One leased attempt: inputs, renewal, execution, upload, ACK and cleanup."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import shutil
import threading
import time

from . import storage
from .client import WorkerClient, LeaseLost
from .config import Config
from .engine.base import Engine, JobContext
from .models import ClaimedJob
from .queues import lease_heartbeat_interval, queue_failure_report

log = logging.getLogger(__name__)


async def _heartbeat_loop(client: WorkerClient, job: ClaimedJob, stop: asyncio.Event, lost: asyncio.Event, cancel=None, progress=None) -> None:
    """Renew the job lease at ~1/3 of the lease window until the job finishes."""
    interval = lease_heartbeat_interval(getattr(job, "task_type", ""), job.lease_seconds)
    renewed = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            try:
                if progress is None:
                    await client.heartbeat(job.id, job.lease_token)
                else:
                    await client.heartbeat(job.id, job.lease_token, progress[0])
                renewed = time.monotonic()
            except LeaseLost:
                lost.set()
                if cancel is not None:
                    cancel.set()
                return
            except Exception:
                log.warning("job %s heartbeat unavailable; retrying", job.id)
                if time.monotonic() - renewed >= job.lease_seconds:
                    lost.set()
                    if cancel is not None:
                        cancel.set()
                    return


async def _process_job(
    cfg: Config,
    client: WorkerClient,
    registry: dict[str, Engine],
    job: ClaimedJob,
) -> None:
    engine = registry.get(job.task_type)
    workdir = cfg.work_dir / job.id / job.lease_token
    stop = asyncio.Event()
    lost = asyncio.Event()
    cancel = threading.Event()
    progress = [0]
    heartbeat = asyncio.create_task(_heartbeat_loop(client, job, stop, lost, cancel, progress if job.task_type == "download" else None))
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        if engine is None:  # shouldn't happen: we only claim our capabilities
            raise RuntimeError(f"no engine for task_type {job.task_type!r}")

        inputs = {}
        for name, url in job.inputs.items():
            if name in ("", ".", "..") or "/" in name or "\\" in name or ":" in name:
                raise ValueError("invalid input name")
            inputs[name] = await storage.download(url, workdir / name, cfg.max_image_bytes)

        ctx = JobContext(
            job_id=job.id,
            task_type=job.task_type,
            params=job.params,
            inputs=inputs,
            workdir=workdir,
            cancel=cancel,
            progress=lambda _status, percent: progress.__setitem__(0, min(99, max(0, int(percent)))),
        )
        # Engines are CPU/GPU-bound and synchronous: run off the event loop.
        inference = asyncio.create_task(asyncio.to_thread(engine.run, ctx))
        try:
            result = await asyncio.shield(inference)
        except asyncio.CancelledError:
            cancel.set()
            # A Python thread cannot be cancelled. Wait until it stops writing
            # before removing its attempt directory or closing shared clients.
            with suppress(Exception):
                await inference
            raise
        if lost.is_set():
            raise LeaseLost("lease expired during processing")
        # Confirm ownership immediately before upload, even after a missed beat.
        await client.heartbeat(job.id, job.lease_token)
        output_limit = engine.result_byte_limit(job.params, cfg.max_result_bytes)
        if output_limit is None:
            # Only the explicit current download media policy is unbounded.
            from .downloader.policy import validate_contract
            from .downloader.safety import ServiceError
            if job.task_type != "download" or job.params.get("operation") != "download":
                raise RuntimeError("INVALID_LIMITS")
            try:
                validate_contract(job.params.get("limits"))
            except ServiceError:
                raise RuntimeError("INVALID_LIMITS") from None
        elif output_limit < 1:
            raise RuntimeError("INVALID_LIMITS")
        await storage.upload(job.result.put_url, result.output_path, result.content_type, output_limit)
        extra = {"duration": result.duration} if job.task_type == "download" and result.duration is not None else {}
        await client.complete(job.id, job.lease_token, job.result.key, result.width, result.height, **extra)
        log.info("job %s done (%dx%d)", job.id, result.width, result.height)
    except LeaseLost:
        log.warning("job %s discarded after lease loss", job.id)
    except Exception as exc:
        failure = (engine.failure_report(exc, job.params) if engine
                   else queue_failure_report(job.task_type, exc, job.params))
        if failure.include_traceback:
            log.exception("job %s failed", job.id)
        else:
            log.error("job %s failed (phase=%s, reason=%s)", job.id, failure.phase, failure.diagnostic_code)
        try:
            await client.fail(job.id, job.lease_token, failure.message)
        except Exception:
            log.warning("could not report failure for job %s", job.id)
    finally:
        stop.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await heartbeat
        shutil.rmtree(workdir, ignore_errors=True)
        with suppress(OSError):
            workdir.parent.rmdir()
