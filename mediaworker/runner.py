"""Compose configuration, warmed engines, node identity and local runtime services."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import signal
from urllib.parse import urlsplit

from .client import WorkerClient, bootstrap_node_token
from .config import Config
from .engine import build_registry
from .keepawake import KeepAwake
from .scheduling import _worker_loop, runtime_capability_groups
from .execution import ExecutionResources

log = logging.getLogger(__name__)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        if not shutdown.is_set():
            log.info("shutdown signal received; finishing in-flight jobs...")
            shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows event loop doesn't support add_signal_handler; fall back.
            signal.signal(sig, lambda *_: _request_stop())


async def run_forever(cfg: Config, *, require_all_engines: bool = False,
                      available_only: bool = False, shutdown: asyncio.Event | None = None,
                      before_polling=None, poll_observer=None, register_node=None) -> None:
    shutdown = shutdown if shutdown is not None else asyncio.Event()
    _install_signal_handlers(shutdown)
    cfg.require_api()
    from .engine.registry import _mock_enabled
    from .model_registry import sync_models

    if _mock_enabled() and urlsplit(cfg.api_base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("mock engine is allowed only with a local API")

    # A dedicated runtime claims one capability subset but registers the
    # complete node capability list so all processes share one stable identity.
    engine_cfg = replace(cfg, capabilities=cfg.claim_capabilities or cfg.capabilities)
    ready = engine_cfg.capabilities if _mock_enabled() else await asyncio.to_thread(sync_models, engine_cfg)
    registry = build_registry(engine_cfg, ready)
    if require_all_engines:
        missing = sorted(set(cfg.capabilities) - set(registry))
        if missing:
            raise SystemExit("required engines unavailable: " + ", ".join(missing))
    if not registry:
        raise SystemExit("no engines loaded — check CAPABILITIES and that models are present")

    if available_only:
        # Only the portable foreground launcher opts into actual readiness.
        cfg.capabilities = list(registry)
        cfg.claim_capabilities = None
    if shutdown.is_set():
        return
    if register_node is None:
        await bootstrap_node_token(cfg)
    else:
        await register_node(cfg)
    if available_only:
        registry = {cap: engine for cap, engine in registry.items() if cap in cfg.capabilities}
        cfg.capabilities = list(registry)
        if not registry:
            raise SystemExit("no locally ready engines are authorized for this node")
    missing_claims = sorted(set(cfg.claim_capabilities or []) - set(cfg.capabilities))
    if missing_claims:
        raise SystemExit("this runtime's claim capabilities are not authorized for the shared node: " + ", ".join(missing_claims))
    if before_polling is not None:
        await before_polling(cfg, registry, shutdown)
    if shutdown.is_set():
        return
    log.info("node %s starting (tier=%s, concurrency=%d, claims=%s)",
             cfg.node_id, cfg.trust_tier, cfg.concurrency, sorted(registry))

    resources = ExecutionResources(cfg, registry)
    log.info("execution capacity task_limit=%d download_limit=%d ai_context_limit=%d (lazy, memory admitted)",
             resources.limit, resources.download_limit, resources.context_limit)
    workers = []
    try:
        with KeepAwake():
            async with WorkerClient(cfg) as client:
                if poll_observer is not None:
                    client.poll_observer = poll_observer
                groups = runtime_capability_groups(cfg, list(registry))
                workers = [
                    asyncio.create_task(_worker_loop(i, cfg, client, registry, shutdown, group, resources=resources))
                    for i, group in enumerate(groups)
                ]
                updater = None
                if cfg.auto_update:
                    from .updater import watch_for_updates

                    updater = asyncio.create_task(watch_for_updates(cfg, shutdown))
                    log.info(
                        "automatic updates enabled (%s/%s every %.0fs)",
                        cfg.auto_update_remote,
                        cfg.auto_update_branch,
                        cfg.auto_update_interval_s,
                    )
                await asyncio.gather(*workers)
                updated = await updater if updater else False
    finally:
        # Never dispose native contexts while a task's execution future owns one.
        shutdown.set()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.to_thread(resources.close)
    log.info("node %s stopped cleanly", cfg.node_id)
    if updated:
        from .updater import RESTART_FOR_UPDATE

        raise SystemExit(RESTART_FOR_UPDATE)
