"""Capability pollers and the host-wide slot pool; no engine-specific work."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.parse import urlsplit

import httpx

from .client import WorkerClient
from .config import Config, MAX_CONCURRENCY
from .engine.base import Engine
from .lifecycle import _process_job
from .single_instance import FileLock, WorkerAlreadyRunning

log = logging.getLogger(__name__)


def _claim_retry_delay(poll_interval_s: float, failures: int) -> float:
    """Bound retry pressure during an outage and avoid synchronized workers."""
    base = min(30.0, max(0.1, poll_interval_s) * 2 ** min(failures - 1, 10))
    return base * random.uniform(0.8, 1.0)


def _claim_error_summary(exc: Exception) -> str:
    # Exception messages may be empty (HTTPX timeouts) or contain credentials,
    # signed URLs or response bodies. The type/status are safe and actionable.
    kind = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{kind} HTTP {exc.response.status_code}"
    return kind


async def _worker_loop(
    idx: int,
    cfg: Config,
    client: WorkerClient,
    registry: dict[str, Engine],
    shutdown: asyncio.Event,
    capabilities: list[str] | None = None,
) -> None:
    if capabilities is None:
        capabilities = cfg.claim_capabilities or list(registry.keys())
    registry = {name: engine for name, engine in registry.items() if name in capabilities}
    models = sorted({model for engine in registry.values() for model in engine.available_models})
    log.info("worker %d online (capabilities=%s)", idx, capabilities)
    failures = 0
    unavailable_since = 0.0
    endpoint = urlsplit(cfg.api_base_url).hostname or "unknown"
    while not shutdown.is_set():
        slot = acquire_worker_slot(cfg.runtime_dir)
        if slot is None:
            await _sleep_or_stop(shutdown, 0.25)
            continue
        delay = cfg.poll_interval_s
        try:
            try:
                job = await client.claim(capabilities, models)
            except Exception as exc:
                if failures == 0:
                    unavailable_since = time.monotonic()
                failures += 1
                delay = _claim_retry_delay(cfg.poll_interval_s, failures)
                log.warning(
                    "worker %d: claim failed (%s; endpoint=%s; failures=%d); next poll in %.1fs",
                    idx, _claim_error_summary(exc), endpoint, failures, delay,
                )
                # An ambiguous claim is never replayed. A later poll is independent.
            else:
                if failures:
                    log.info(
                        "worker %d: claim connection recovered (endpoint=%s; failures=%d; unavailable=%.1fs)",
                        idx, endpoint, failures, time.monotonic() - unavailable_since,
                    )
                    failures = 0
                if job is not None:
                    # Finish the attempt before releasing its host-wide slot.
                    try:
                        await _process_job(cfg, client, registry, job)
                    except Exception:
                        log.exception("worker %d: unexpected job error; continuing", idx)
                    continue
        finally:
            # Cancellation, empty claims and errors all return ownership here.
            slot.release()
        await _sleep_or_stop(shutdown, delay)
    log.info("worker %d stopped", idx)


def acquire_worker_slot(runtime_dir, max_slots: int = MAX_CONCURRENCY) -> FileLock | None:
    """Acquire one crash-safe slot shared by every runtime on this machine."""
    slot_dir = runtime_dir / "slots"
    for index in range(max_slots):
        try:
            return FileLock(slot_dir / f"slot-{index}.lock").acquire()
        except WorkerAlreadyRunning:
            continue
    return None


def worker_capability_groups(capabilities: list[str], concurrency: int) -> list[list[str]]:
    """Keep the download API poller separate while sharing the host slot pool."""
    image_capabilities = [capability for capability in dict.fromkeys(capabilities) if capability != "download"]
    groups = [[*image_capabilities] for _ in range(max(1, concurrency))] if image_capabilities else []
    if "download" in capabilities:
        groups.append(["download"])
    return groups


async def _sleep_or_stop(shutdown: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def runtime_capability_groups(cfg: Config, capabilities: list[str]) -> list[list[str]]:
    """Separate queue protocols without exceeding one download poller per runtime."""
    if cfg.claim_capabilities:
        if "download" in cfg.claim_capabilities:
            if set(cfg.claim_capabilities) != {"download"}:
                raise SystemExit("WORKER_CLAIM_CAPABILITIES must isolate download from image tasks")
            return [["download"]]
        return [list(cfg.claim_capabilities) for _ in range(cfg.concurrency)]
    return worker_capability_groups(capabilities, cfg.concurrency)
