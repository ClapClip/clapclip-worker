"""One fixed-release child, a closed admission gate, and cooperative draining."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time

from .api import Bootstrap, configured_origin
from .common import ManagedError, inside, read_json, write_json
from .credentials import Login, load
from .host import data_root
from .releases import executable, tool_environment


def configuration(root: Path, value: dict, login: Login):
    from ..deployment import configuration as clean_configuration
    with clean_configuration(None) as cfg:
        # Managed state cannot silently restore an old business-site API or
        # ambient API_BASE_URL. Every control/queue route uses this one backend.
        bootstrap = Bootstrap.validate(configured_origin(), value["bootstrap"])
        cfg.api_base_url = bootstrap.origin
        cfg.node_id = read_json(root / "host.json")["node_id"]
        cfg.node_token = ""
        cfg.cognito_username, cfg.cognito_password = login.username, login.password
        cfg.cognito_user_pool_id, cfg.cognito_client_id = bootstrap.pool, bootstrap.client
        cfg.cognito_region = bootstrap.region
        cfg.capabilities, cfg.claim_capabilities = value["capabilities"], None
        cfg.concurrency = 30
        data = data_root(root)
        cfg.runtime_dir, cfg.node_token_file = data / "node", data / "node/node-token.json"
        models = inside(data / "models", value["model_set"])
        cfg.models_dir, cfg.faceswap_models_dir = models, models / "faceswap"
        cfg.cutout_models_dir, cfg.enhance_models_dir = models / "cutout", models / "enhance"
        cfg.work_dir = data / "work"
        cfg.auto_update = cfg.faceswap_model_auto_download = cfg.cutout_model_auto_download = False
        # This named profile never silently becomes a DirectML/CUDA deployment.
        cfg.onnx_providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"] if os.sys.platform == "darwin" else ["CPUExecutionProvider"]
        return cfg


def public_environment(root: Path) -> dict:
    from ..deployment import configuration as clean_configuration
    with clean_configuration(None):
        env = tool_environment(root)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    env["LOG_FILE"] = str(data_root(root) / "worker.log")
    for name, tool in (("DOWNLOAD_NODE", "node"), ("DOWNLOAD_FFMPEG", "ffmpeg"), ("DOWNLOAD_FFPROBE", "ffprobe")):
        env[name] = executable(root / "tools", tool)
    return env


class RuntimeEvents:
    def __init__(self, directory: Path, session: str, revision: str):
        self.directory, self.session, self.revision = directory, session, revision
        self.state = {"session": session, "revision": revision, "pid": os.getpid()}

    def record(self, stage: str, **fields):
        self.state.update(stage=stage, observed_at=time.time(), **fields)
        write_json(self.directory / "worker.json", self.state)

    def requested(self, name: str) -> bool:
        path = self.directory / (name + ".json")
        return path.is_file() and read_json(path).get("session") == self.session

    def manager_alive(self) -> bool:
        from .host import Process
        from .processes import same_process
        try:
            owner = read_json(self.directory / "manager.json")["identity"]
            return same_process(Process(**owner))
        except (ManagedError, OSError, TypeError, KeyError):
            return False

    async def watch(self, shutdown: asyncio.Event):
        while not shutdown.is_set():
            if self.stop_if_unowned(shutdown):
                return
            await asyncio.sleep(0.1)

    def stop_if_unowned(self, shutdown: asyncio.Event) -> bool:
        alive = self.manager_alive()
        try:
            drain = self.requested("drain")
        except (ManagedError, OSError):
            drain = True
        if drain or not alive:
            # Set first: even an unwritable status file must stop new claims.
            shutdown.set()
            try:
                self.record("draining", manager_alive=alive)
            except (ManagedError, OSError):
                pass
            return True
        return False

    async def before_polling(self, cfg, registry, shutdown):
        self.record("registered_gate_closed", capabilities=sorted(registry),
                    models={name: engine.available_models for name, engine in registry.items()})
        while not shutdown.is_set():
            if self.stop_if_unowned(shutdown):
                return
            if self.requested("allow-claims"):
                self.record("polling_enabled")
                return
            await asyncio.sleep(0.1)

    def polled(self, capabilities):
        groups = self.state.setdefault("accepted_poll_groups", [])
        if sorted(capabilities) not in groups:
            groups.append(sorted(capabilities))
        self.record("queue_poll_accepted", last_poll_accepted=time.time())


async def worker(root: Path, revision: str, session: str) -> None:
    from ..runner import run_forever
    from ..__main__ import _configure_logging
    directory = inside(data_root(root) / "sessions", session)
    value = read_json(directory / "configuration.json")
    login = load(root, read_json(root / "host.json")["credential"])
    cfg = configuration(root, value, login)
    env = public_environment(root)
    os.environ.clear()
    os.environ.update(env)
    _configure_logging()
    events = RuntimeEvents(directory, session, revision)
    shutdown = asyncio.Event()
    monitor = asyncio.create_task(events.watch(shutdown))
    events.record("loading")
    try:
        async def register(cfg):
            if not events.manager_alive():
                raise ManagedError("RUNTIME_MANAGER_IDENTITY_LOST")
            from ..client import bootstrap_node_token, _read_shared_token
            cached = _read_shared_token(cfg)
            await bootstrap_node_token(cfg, force=True, previous_token=cached[0] if cached else None)
        await run_forever(cfg, require_all_engines=True, available_only=True, shutdown=shutdown,
                          before_polling=events.before_polling, poll_observer=events.polled,
                          register_node=register)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        events.record("exited")


def inspect(root: Path, value: dict) -> dict:
    from ..engine import build_registry
    cfg = configuration(root, value, Login("offline-engine-check", "not-used"))
    registry = build_registry(cfg, cfg.capabilities)
    ready = sorted(registry)
    if ready != sorted(cfg.capabilities):
        raise ManagedError("LOCAL_ENGINE_VALIDATION_FAILED")
    return {"capabilities": ready, "models_loaded": True, "registered": False}
