"""Single-owner release state machine. Preparation never mutates the live tree."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sys
import time
import threading
import uuid

from .api import SetupAPI, configured_origin
from .common import ManagedError, inside, read_json, write_json
from .credentials import load
from .host import HostLock, data_root, require_no_other_worker
from .processes import Child, manager_family, identity, same_process
from .releases import Releases, command, venv_python
from .runtime import public_environment

UPDATE_SECONDS = 3600
STARTUP_SECONDS = 600
DRAIN_SECONDS = 1800


def prepare_configuration(root: Path, release: Path, api: SetupAPI, *, inspect_engines=True) -> dict:
    host = read_json(root / "host.json")
    # portable-cpu-v1 deliberately excludes enhance (requires DirectML/CUDA).
    allowed = api.preflight(host["node_id"], ["faceswap", "cutout", "download"])
    binding = api.account_binding()
    if host.get("account_binding") and host["account_binding"] != binding:
        raise ManagedError("ACCOUNT_CHANGE_REQUIRES_REVIEW")
    assets = api.assets(allowed)
    models = api.prepare_models(data_root(root) / "models", assets)
    b = api.bootstrap
    value = {"bootstrap": {"version": 1, "apiOrigin": b.origin,
             "cognito": {"userPoolId": b.pool, "clientId": b.client, "region": b.region},
             "modelsEndpoint": b.models_path}, "capabilities": allowed, "model_set": models.name}
    path = data_root(root) / "prepared-configuration.json"
    write_json(path, value)
    if inspect_engines:
        command([str(venv_python(release / ".venv-managed")), "-B", "-m", "mediaworker.managed", "inspect",
                 "--root", str(root), "--configuration", str(path)], cwd=release, env=public_environment(root), timeout=600)
    return value


class Agent:
    def __init__(self, root: Path, *, releases=None, child_factory=Child, clock=time.monotonic, sleep=time.sleep):
        self.root, self.data = root, data_root(root)
        self.releases = releases if releases is not None else Releases(root)
        self.child_factory, self.clock, self.sleep = child_factory, clock, sleep
        self.child = None
        self.session = None
        self.stop_requested = False
        self.monitor_done = threading.Event()
        self.permitted = manager_family(root)
        self.parents = [identity(pid) for pid in self.permitted if pid != os.getpid()]

    def status(self, stage, **fields):
        write_json(self.data / "agent.json", {"pid": os.getpid(), "manager": asdict(identity(os.getpid())), "stage": stage,
                   "session": self.session, "observed_at": time.time(), **fields})

    def control(self):
        path = self.data / "control.json"
        return read_json(path) if path.exists() else {}

    def inventory(self):
        if any(not same_process(p) for p in self.parents):
            raise ManagedError("MANAGER_PARENT_IDENTITY_LOST")
        owned = set(self.child.refresh()) if self.child else set()
        require_no_other_worker(permitted=self.permitted | owned)
        if self.child:
            write_json(self.data / "sessions" / self.session / "processes.json",
                       {"identities": [asdict(p) for p in self.child.refresh().values()]})

    def request_drain(self):
        if self.child:
            write_json(self.data / "sessions" / self.session / "drain.json", {"session": self.session})

    def watch_control(self):
        # Preparation may be downloading gigabytes. Persistent stop must still
        # reach the live worker promptly; this thread never spawns or kills one.
        while not self.monitor_done.wait(0.2):
            try:
                if any(not same_process(p) for p in self.parents):
                    self.stop_requested = True
                if self.stop_requested or self.control().get("paused"):
                    self.request_drain()
            except (ManagedError, OSError):
                self.stop_requested = True
                self.request_drain()

    def drain(self, *, timeout=DRAIN_SECONDS):
        if not self.child:
            return
        self.status("draining", revision=self.child.revision)
        self.request_drain()
        deadline = self.clock() + timeout
        while self.child.alive():
            if self.clock() >= deadline:
                self.status("drain_waiting_for_operator", code="IN_FLIGHT_DRAIN_TIMEOUT")
                raise ManagedError("IN_FLIGHT_DRAIN_TIMEOUT")
            self.sleep(0.2)
        self.child = None

    def start_gated(self, selection: dict):
        if self.child is not None:
            raise ManagedError("OLD_WORKER_NOT_FULLY_EXITED")
        self.inventory()
        revision = selection["revision"]
        self.releases.verify_fixed(revision)
        self.session = uuid.uuid4().hex
        directory = self.data / "sessions" / self.session
        write_json(directory / "configuration.json", selection["configuration"])
        write_json(directory / "manager.json", {"identity": asdict(identity(os.getpid()))})
        # Persist before spawn: a crash gap cannot be mistaken for permission
        # to start another child. Unknown orphan identity fails closed.
        write_json(self.data / "active-session.json", {"session": self.session, "revision": revision})
        self.child = self.child_factory(self.root, self.releases.release(revision), self.session, revision,
                                        public_environment(self.root))
        self.inventory()
        self.status("candidate_gate_closed", revision=revision)
        deadline = self.clock() + STARTUP_SECONDS
        while self.child.alive():
            self.inventory()
            if self.stop_requested or self.control().get("paused"):
                raise ManagedError("START_CANCELLED")
            ready = directory / "worker.json"
            if ready.is_file():
                value = read_json(ready)
                if (value.get("session") == self.session and value.get("revision") == revision
                        and value.get("stage") == "registered_gate_closed"
                        and value.get("capabilities")
                        and set(value["capabilities"]) <= set(selection["configuration"]["capabilities"])
                        and self.child.ready_identity(value.get("pid"))):
                    return
            if self.clock() >= deadline:
                raise ManagedError("CANDIDATE_STARTUP_TIMEOUT")
            self.sleep(0.2)
        raise ManagedError("CANDIDATE_STARTUP_FAILED")

    def discard_gated(self):
        if not self.child:
            return
        self.request_drain()
        deadline = self.clock() + 10
        while self.child.alive() and self.clock() < deadline:
            self.sleep(0.2)
        if self.child.alive():
            self.child.terminate_gated()
            deadline = self.clock() + 10
            while self.child.alive() and self.clock() < deadline:
                self.sleep(0.2)
        if self.child.alive():
            raise ManagedError("CANDIDATE_IDENTITY_OR_EXIT_UNCONFIRMED")
        self.child = None

    def activate(self, selection: dict, previous: dict | None):
        self.drain()
        try:
            self.start_gated(selection)
            if self.stop_requested or self.control().get("paused"):
                raise ManagedError("START_CANCELLED")
        except BaseException:
            self.discard_gated()
            if previous and not self.stop_requested and not self.control().get("paused"):
                self.start_gated(previous)
                write_json(self.data / "selected.json", previous)
                write_json(self.data / "sessions" / self.session / "allow-claims.json", {"session": self.session})
                self.status("rollback_running", revision=previous["revision"])
            raise
        # Commit first, then admit. Crash recovery always knows the selected
        # complete release; admission is a session-bound file, never an env flag.
        write_json(self.data / "selected.json", selection)
        write_json(self.data / "sessions" / self.session / "allow-claims.json", {"session": self.session})
        self.status("running", revision=selection["revision"])

    def recover(self):
        path = self.data / "active-session.json"
        if not path.exists():
            self.inventory()
            return
        from .processes import identity, same_process
        from .host import Process, inventory
        active = read_json(path)
        directory = inside(self.data / "sessions", active["session"])
        records = directory / "processes.json"
        known = [Process(**v) for v in read_json(records)["identities"]] if records.exists() else []
        alive = {record.pid: record for record in known if same_process(record)}
        # Exact OS identities from this manager's durable session only. Never
        # adopt an unrelated Python or a process whose PID has been recycled.
        found = inventory(exclude=self.permitted)
        if any(p.pid not in alive for p in found):
            raise ManagedError("ORPHAN_IDENTITY_REQUIRES_REVIEW")
        write_json(directory / "drain.json", {"session": active["session"]})
        deadline = self.clock() + DRAIN_SECONDS
        while any(same_process(record) for record in alive.values()):
            if self.clock() >= deadline:
                raise ManagedError("IN_FLIGHT_DRAIN_TIMEOUT")
            self.sleep(0.2)
        self.inventory()

    def update(self, selected):
        revision = self.releases.latest(selected["revision"])
        failed_path = self.data / "failed-update.json"
        if failed_path.exists():
            failed = read_json(failed_path)
            if failed.get("revision") == revision and failed.get("retry_after", 0) > time.time():
                return selected
        if revision == selected["revision"]:
            return selected
        self.status("preparing_update", revision=selected["revision"], candidate=revision)
        try:
            release = self.releases.prepare(revision)
            login = load(self.root, read_json(self.root / "host.json")["credential"])
            api = SetupAPI(SetupAPI.discover(configured_origin()), login)
            value = prepare_configuration(self.root, release, api)
            candidate = {"revision": revision, "configuration": value}
            if self.control().get("paused") or self.stop_requested:
                return selected
            self.activate(candidate, selected)
            write_json(self.data / "manager-handoff.json", {"candidate": revision, "previous": selected,
                                                            "acknowledged": False})
            return candidate
        except ManagedError as error:
            write_json(failed_path, {"revision": revision, "code": error.code, "retry_after": time.time() + 86400})
            if self.child is None or not self.child.alive():
                raise
            self.status("update_failed_old_running", revision=selected["revision"], code=error.code)
            return selected

    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stop_requested", True))
        with HostLock(self.root):
            monitor = threading.Thread(target=self.watch_control, daemon=True)
            monitor.start()
            try:
                return self._run_locked()
            finally:
                try:
                    self.drain_before_unlock()
                finally:
                    self.monitor_done.set()
                    monitor.join(timeout=1)

    def drain_before_unlock(self):
        # Every exit (including unexpected errors) keeps the host lock until
        # the exact child family exits. Unverifiable identity is never an exit
        # receipt, and this path never kills an admitted worker.
        while self.child is not None:
            try:
                self.request_drain()
                if not self.child.alive():
                    self.child = None
                    return
            except (ManagedError, OSError):
                pass
            self.sleep(0.5)

    def _run_locked(self):
        self.recover()
        if self.control().get("halted"):
            self.status("halted_requires_resume")
            return 78
        selected = read_json(self.data / "selected.json")
        failures, next_update = 0, self.clock() + UPDATE_SECONDS
        while not self.stop_requested:
            if self.control().get("paused"):
                self.drain()
                self.status("paused", revision=selected["revision"])
                self.sleep(0.5)
                continue
            if self.child is None or not self.child.alive():
                self.child = None
                try:
                    self.activate(selected, None)
                    failures = 0
                except ManagedError as error:
                    failures += 1
                    if failures >= 4:
                        write_json(self.data / "control.json", {"halted": True, "code": error.code})
                        self.status("halted_requires_resume", code=error.code)
                        return 78
                    self.status("startup_backoff", code=error.code, failures=failures)
                    deadline = self.clock() + min(900, 60 * 2**(failures - 1))
                    while self.clock() < deadline and not self.stop_requested and not self.control().get("paused"):
                        self.sleep(0.5)
                    continue
            self.inventory()
            handoff = self.data / "manager-handoff.json"
            if handoff.exists():
                value = read_json(handoff)
                own_revision = Path(__file__).resolve().parents[2].name
                if own_revision == value.get("candidate") and not value.get("acknowledged"):
                    write_json(handoff, {**value, "acknowledged": True})
            if self.clock() >= next_update:
                try:
                    previous = selected
                    selected = self.update(selected)
                    if selected["revision"] != previous["revision"]:
                        # Update the manager itself through the OS service,
                        # with all workers drained. No Python hot reload.
                        self.drain()
                        self.status("manager_restart_for_update", revision=selected["revision"])
                        return 75
                except ManagedError as error:
                    self.status("update_check_failed", code=error.code, revision=selected["revision"])
                next_update = self.clock() + UPDATE_SECONDS
            self.sleep(0.5)
        self.drain()
        self.status("stopped", revision=selected["revision"])
        return 0
