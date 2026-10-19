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


async def run_forever(cfg: Config, *, require_all_engines: bool = False) -> None:
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

    await bootstrap_node_token(cfg)
    missing_claims = sorted(set(cfg.claim_capabilities or []) - set(cfg.capabilities))
    if missing_claims:
        raise SystemExit("this runtime's claim capabilities are not authorized for the shared node: " + ", ".join(missing_claims))
    log.info("node %s starting (tier=%s, concurrency=%d, claims=%s)",
             cfg.node_id, cfg.trust_tier, cfg.concurrency, sorted(registry))

    with KeepAwake():
        async with WorkerClient(cfg) as client:
            shutdown = asyncio.Event()
            _install_signal_handlers(shutdown)
            groups = runtime_capability_groups(cfg, list(registry))
            workers = [
                asyncio.create_task(_worker_loop(i, cfg, client, registry, shutdown, group))
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
    log.info("node %s stopped cleanly", cfg.node_id)
    if updated:
        from .updater import RESTART_FOR_UPDATE

        raise SystemExit(RESTART_FOR_UPDATE)
