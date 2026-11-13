"""One leased attempt: inputs, renewal, execution, upload, ACK and cleanup."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import logging
import shutil
import threading
import time

from . import storage
from .downloader.timings import record as record_download_timing
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
    *, executor=None,
) -> None:
    engine = registry.get(job.task_type)
    workdir = cfg.work_dir / job.id / job.lease_token
    stop = asyncio.Event()
    lost = asyncio.Event()
    cancel = threading.Event()
    progress = [0]
    is_gif = job.task_type == "faceswap" and job.params.get("targetMedia") == "gif"
    reports_progress = job.task_type == "download" or is_gif
    heartbeat = asyncio.create_task(_heartbeat_loop(client, job, stop, lost, cancel, progress if reports_progress else None))
    transfer = None
    owned_executor = None
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        if engine is None:  # shouldn't happen: we only claim our capabilities
            raise RuntimeError(f"no engine for task_type {job.task_type!r}")

        phase = time.perf_counter()
        if is_gif:
            transfer = storage.transfer_client()
        inputs = await _transfer_while_owned(
            _download_inputs(job.inputs, workdir, cfg.max_image_bytes, transfer), lost)
        download_seconds = time.perf_counter() - phase

        if lost.is_set():
            raise LeaseLost("lease expired before processing")
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
        download_engine_start=time.monotonic()
        if job.task_type=="download":record_download_timing("engine",download_engine_start,state="begin")
        phase = time.perf_counter()
        if executor is None:
            owned_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="leased-task")
            executor = owned_executor
        # Keep the concurrent Future: cancelling an asyncio wrapper does not
        # prove a native worker thread exited.
        native_inference = executor.submit(engine.run, ctx)
        inference = asyncio.wrap_future(native_inference)
        try:
            result = await asyncio.shield(inference)
        except asyncio.CancelledError:
            cancel.set()
            # A Python thread cannot be cancelled. Wait until it stops writing
            # before removing its attempt directory or closing shared clients.
            while not native_inference.done():
                try:
                    if inference.cancelled():
                        inference = asyncio.wrap_future(native_inference)
                    await asyncio.shield(inference)
                except asyncio.CancelledError:
                    # Repeated task.cancel() must not cancel the join or free
                    # the context/attempt while native Run still writes.
                    cancel.set()
                except Exception:
                    break
            def consume_joined_error(future):
                if not future.cancelled():
                    with suppress(BaseException):
                        future.exception()
            # The native Future may finish just before wrap_future schedules
            # its result on the loop; consume that eventual exception too.
            inference.add_done_callback(consume_joined_error)
            raise
        process_seconds = time.perf_counter() - phase
        if job.task_type=="download":record_download_timing("engine",download_engine_start,output_path=result.output_path)
        if lost.is_set():
            raise LeaseLost("lease expired during processing")
        # Confirm ownership immediately before upload, even after a missed beat.
        preupload_start=time.monotonic()
        if job.task_type=="download":record_download_timing("preupload",preupload_start,state="begin")
        await client.heartbeat(job.id, job.lease_token)
        if job.task_type=="download":record_download_timing("preupload",preupload_start)
        if lost.is_set():
            raise LeaseLost("lease expired before upload")
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
        output_bytes = result.output_path.stat().st_size if is_gif else 0
        phase = time.perf_counter()
        options = {"client": transfer} if transfer is not None else {}
        upload_start=time.monotonic()
        if job.task_type=="download":record_download_timing("upload",upload_start,state="begin",input_path=result.output_path)
        await _transfer_while_owned(
            storage.upload(job.result.put_url, result.output_path, result.content_type, output_limit, **options), lost)
        upload_seconds = time.perf_counter() - phase
        if job.task_type=="download":record_download_timing("upload",upload_start,input_path=result.output_path)
        extra = {"duration": result.duration} if job.task_type == "download" and result.duration is not None else {}
        phase = time.perf_counter()
        if lost.is_set():
            raise LeaseLost("lease expired before completion")
        complete_start=time.monotonic()
        if job.task_type=="download":record_download_timing("complete",complete_start,state="begin")
        await client.complete(job.id, job.lease_token, job.result.key, result.width, result.height, **extra)
        if job.task_type=="download":record_download_timing("complete",complete_start)
        # Observability must never send fail after an accepted terminal ACK.
        with suppress(Exception):
            if is_gif:
                log.info("job %s GIF timings download=%.3fs process=%.3fs upload=%.3fs complete=%.3fs result_bytes=%d",
                         job.id, download_seconds, process_seconds, upload_seconds, time.perf_counter() - phase, output_bytes)
            log.info("job %s done (%dx%d)", job.id, result.width, result.height)
    except LeaseLost:
        log.warning("job %s discarded after lease loss", job.id)
    except Exception as exc:
        if lost.is_set():
            # A cancelled engine may throw its own error after fencing. Never
            # report a business failure against a lease we no longer own.
            log.warning("job %s discarded after lease loss", job.id)
            return
        failure = (engine.failure_report(exc, job.params) if engine
                   else queue_failure_report(job.task_type, exc, job.params))
        if failure.include_traceback:
            log.exception("job %s failed", job.id)
        else:
            log.error("job %s failed (phase=%s, reason=%s)", job.id, failure.phase, failure.diagnostic_code)
        try:
            metadata = ({"phase": failure.phase, "diagnostic_code": failure.diagnostic_code}
                        if job.task_type == "download" else {})
            await client.fail(job.id, job.lease_token, failure.message, **metadata)
        except Exception:
            log.warning("could not report failure for job %s", job.id)
    finally:
        if owned_executor is not None:
            owned_executor.shutdown(wait=True, cancel_futures=False)
        stop.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await heartbeat
        if transfer is not None:
            with suppress(Exception):
                await transfer.aclose()
        shutil.rmtree(workdir, ignore_errors=True)
        with suppress(OSError):
            workdir.parent.rmdir()


async def _download_inputs(urls, workdir, max_bytes, transfer):
    for name in urls:
        if name in ("", ".", "..") or "/" in name or "\\" in name or ":" in name:
            raise ValueError("invalid input name")
    if transfer is None:
        return {name: await storage.download(url, workdir / name, max_bytes) for name, url in urls.items()}
    pending = [asyncio.create_task(storage.download(url, workdir / name, max_bytes, client=transfer))
               for name, url in urls.items()]
    try:
        paths = await asyncio.gather(*pending)
        return dict(zip(urls, paths))
    finally:
        # gather does not cancel siblings on error. Stop and await every writer
        # before the attempt's directory or anonymous connection pool is closed.
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _transfer_while_owned(transfer, lost):
    """Stop async I/O on fencing; join all writers before attempt cleanup."""
    pending = asyncio.create_task(transfer)
    fenced = asyncio.create_task(lost.wait())
    try:
        await asyncio.wait((pending, fenced), return_when=asyncio.FIRST_COMPLETED)
        if lost.is_set():
            raise LeaseLost("lease expired during transfer")
        return await pending
    finally:
        for task in (pending, fenced):
            if not task.done():
                task.cancel()
        await asyncio.gather(pending, fenced, return_exceptions=True)
