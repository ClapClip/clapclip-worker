"""No-argument enrollment orchestration; OS changes follow offline readiness."""
from __future__ import annotations

import getpass
import json
from pathlib import Path
import sys
import time
import uuid
import warnings

from .agent import prepare_configuration
from .api import SetupAPI, configured_origin
from .common import ManagedError, read_json, write_json
from .credentials import Login, store
from .host import (administrator, data_root, handoff_data, host_root, install_service,
                   protect_new_root, require_no_other_worker, run_checked, service_plan, service_running, service_user)
from .releases import PROFILE, Releases, tools_python
from ..single_instance import FileLock, WorkerAlreadyRunning

LAUNCHER = '''"""Protected boot entry. No credentials, network access or mutable root imports."""
import json
import os
from pathlib import Path
import re
import subprocess
import signal
import sys
root = Path(__file__).resolve().parent
host = json.loads((root / "host.json").read_text(encoding="utf-8"))
data = Path("/var/lib/clapclip-worker-runtime") if sys.platform.startswith("linux") and host["service_user"] == "dynamic" else root / "data"
selection = json.loads((data / "selected.json").read_text(encoding="utf-8"))
revision = selection["revision"]
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit(78)
release = data / "releases" / revision
python = release / ".venv-managed" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
arguments = [str(python), "-B", "-m", "mediaworker.managed", "agent", "--root", str(root)]
child = subprocess.Popen(arguments, cwd=release)
def stop(*_):
    if sys.platform != "win32" and child.poll() is None:
        child.send_signal(signal.SIGTERM)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, stop)
result = child.wait()
# A newly installed manager must acknowledge startup. On failure before that
# acknowledgement restore its last validated selection; its previous manager
# recovers/drains exact durable process identities before starting anything.
handoff = data / "manager-handoff.json"
if result and handoff.is_file():
    trial = json.loads(handoff.read_text(encoding="utf-8"))
    if trial.get("candidate") == revision and not trial.get("acknowledged"):
        temporary = data / ("selected.rollback." + str(os.getpid()))
        temporary.write_text(json.dumps(trial["previous"]), encoding="utf-8")
        os.replace(temporary, data / "selected.json")
        control = data / "control.json"
        if control.is_file():
            value = json.loads(control.read_text(encoding="utf-8"))
            if value.get("halted"):
                value["halted"] = False
                temporary = data / ("control.rollback." + str(os.getpid()))
                temporary.write_text(json.dumps(value), encoding="utf-8")
                os.replace(temporary, control)
        handoff.unlink()
        raise SystemExit(75)
raise SystemExit(result)
'''


def owned_host(root: Path) -> dict:
    host = read_json(root / "host.json")
    if host.get("owner") != "clapclip-managed-v1" or host.get("profile") != PROFILE:
        raise ManagedError("HOST_DIRECTORY_NOT_OWNED")
    return host


def hidden_login() -> Login:
    username = input("Worker account: ").strip()
    # Do not allow getpass to fall back to visibly echoed input on a bad TTY.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("Password (hidden): ")
        except getpass.GetPassWarning:
            raise ManagedError("SECURE_INTERACTIVE_TERMINAL_REQUIRED") from None
    login = Login(username, password)
    login.encode()
    return login


def install(root: Path | None = None, *, api_factory=SetupAPI, releases=None, prompt=hidden_login,
            inspect_engines=True, wait_seconds=90) -> dict:
    root = root or host_root()
    if not administrator():
        raise ManagedError("ADMINISTRATOR_REQUIRED_FOR_BOOT_SERVICE")
    marker = root / "bootstrap-owner.txt"
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != "clapclip-bootstrap-v1":
        raise ManagedError("HOST_DIRECTORY_NOT_OWNED")
    try:
        with FileLock(root / "install.lock"):
            return _install(root, api_factory=api_factory, releases=releases, prompt=prompt,
                            inspect_engines=inspect_engines, wait_seconds=wait_seconds)
    except WorkerAlreadyRunning:
        raise ManagedError("INSTALL_ALREADY_RUNNING") from None


def _install(root: Path, *, api_factory, releases, prompt, inspect_engines, wait_seconds) -> dict:
    record = root / "host.json"
    if record.exists():
        host = owned_host(root)
        # Reuse a known managed installation without a second account/node.
        if host.get("credential") and (data_root(root) / "selected.json").exists():
            from .control import resume, status
            resume(root)
            return status(root)
    else:
        require_no_other_worker()
        user = service_user()
        host = {"owner": "clapclip-managed-v1", "profile": PROFILE, "service_user": user,
                "node_id": "worker-" + uuid.uuid4().hex}
        # Bootstrap has already made this exact root administrator-owned.
        marker = root / "bootstrap-owner.txt"
        if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != "clapclip-bootstrap-v1":
            raise ManagedError("HOST_DIRECTORY_NOT_OWNED")
        write_json(record, host)
    protect_new_root(root, host["service_user"])
    if sys.platform != "win32":
        record.chmod(0o644)
    require_no_other_worker()
    manager = releases if releases is not None else Releases(root)
    revision = manager.latest()
    release = manager.prepare(revision)
    write_json(data_root(root) / "install-status.json", {"stage": "dependencies_validated", "revision": revision})
    # Discovery failure (including not-yet-deployed APIs) precedes password input,
    # credential storage, registration and boot-service installation.
    bootstrap = api_factory.discover(configured_origin())
    login = prompt()
    api = api_factory(bootstrap, login)
    configuration = prepare_configuration(root, release, api, inspect_engines=inspect_engines)
    host["account_binding"] = api.account_binding()
    host["credential"] = store(root, login)
    write_json(record, host)
    if sys.platform != "win32":
        record.chmod(0o644)  # Only public identity/reference; credential is separate.
    launcher = root / "clapclip-agent.py"
    if launcher.exists() and launcher.read_text(encoding="utf-8") != LAUNCHER:
        raise ManagedError("EXISTING_BOOT_LAUNCHER_REQUIRES_REVIEW")
    launcher.write_text(LAUNCHER, encoding="utf-8")
    if sys.platform != "win32":
        launcher.chmod(0o644)
    helper = release / "scripts/worker-control.py"
    if helper.is_file():
        (root / "worker-control.py").write_bytes(helper.read_bytes())
        if sys.platform == "win32":
            (root / "worker.cmd").write_text('@echo off\n"%~dp0tools\\python.exe" -B "%~dp0worker-control.py" %*\n', encoding="utf-8")
        else:
            control = root / "worker"
            control.write_text('#!/bin/sh\nroot=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$root/tools/bin/python" -B "$root/worker-control.py" "$@"\n', encoding="utf-8")
            control.chmod(0o755)
    write_json(data_root(root) / "selected.json", {"revision": revision, "configuration": configuration})
    write_json(data_root(root) / "control.json", {"paused": False, "halted": False})
    handoff_data(root, host["service_user"])
    plan = service_plan(root, tools_python(root / "tools"), host["service_user"], host["credential"])
    install_service(plan)
    write_json(data_root(root) / "install-status.json", {"stage": "boot_registration_installed", "revision": revision})
    handoff_data(root, host["service_user"])
    run_checked(list(plan.start))
    # Independently query OS + durable manager/child identity after start returns.
    from .control import status
    deadline = time.monotonic() + wait_seconds
    report = status(root)
    while time.monotonic() < deadline and not (report.get("background_alive") and report.get("queue_poll_accepted")):
        if report.get("stage") == "halted_requires_resume":
            break
        time.sleep(0.5)
        report = status(root)
    if host["credential"].get("notice"):
        report["credential_storage_notice"] = host["credential"]["notice"]
    return report
