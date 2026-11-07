"""Public status and persistent cooperative control of an owned installation."""
from __future__ import annotations

import os
from pathlib import Path
import time

from .common import ManagedError, inside, read_json, write_json
from .host import administrator, data_root, run_checked, service_plan, service_running
from .processes import same_process
from .host import Process
from .releases import tools_python


def _host(root):
    from .setup import owned_host
    return owned_host(root)


def status(root: Path) -> dict:
    host = _host(root)
    data = data_root(root)
    selected = read_json(data / "selected.json") if (data / "selected.json").exists() else {}
    agent = read_json(data / "agent.json") if (data / "agent.json").exists() else {}
    result = {"node_id": host["node_id"], "version": selected.get("revision"), "profile": host["profile"],
              "stage": agent.get("stage", "not_started"), "background_alive": False,
              "registered": False, "queue_poll_accepted": False, "media_task_success": "not_verified"}
    manager_alive = bool(agent.get("manager") and same_process(Process(**agent["manager"])))
    result["manager_alive"] = manager_alive
    try:
        result["os_service_running"] = service_running()
    except ManagedError:
        result["os_service_running"] = False
    session = agent.get("session")
    if session:
        directory = inside(data / "sessions", session)
        if (directory / "worker.json").exists():
            worker = read_json(directory / "worker.json")
            records = read_json(directory / "processes.json").get("identities", []) if (directory / "processes.json").exists() else []
            alive = {p["pid"] for p in records if same_process(Process(**p))}
            current = worker.get("session") == session and worker.get("revision") == selected.get("revision")
            result["background_alive"] = bool(manager_alive and current and result["os_service_running"] and worker.get("pid") in alive)
            result["registered"] = bool(result["background_alive"] and worker.get("capabilities"))
            result["capabilities"] = worker.get("capabilities", [])
            result["queue_poll_accepted"] = bool(result["background_alive"] and worker.get("accepted_poll_groups"))
            result["last_poll_accepted"] = worker.get("last_poll_accepted")
            result["poll_recent"] = bool(result["queue_poll_accepted"] and time.time() - worker.get("last_poll_accepted", 0) < 120)
    plan = service_plan(root, tools_python(root / "tools"), host["service_user"], host.get("credential"))
    result["boot_definition_present"] = plan.path.is_file() and plan.path.read_bytes() == plan.content
    from .host import service_registered
    result["boot_registered_enabled"] = service_registered(plan)
    return result


def stop(root: Path, *, timeout=1800):
    _host(root)
    data = data_root(root)
    write_json(data / "control.json", {"paused": True, "halted": False})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = status(root)
        if not report["background_alive"] or report["stage"] in {"paused", "stopped", "halted_requires_resume"} or not report["os_service_running"]:
            # A stopped OS service can leave orphans; confirm every durable child.
            active = data / "active-session.json"
            if active.exists():
                session = read_json(active)["session"]
                records = inside(data / "sessions", session) / "processes.json"
                if records.exists() and any(same_process(Process(**p)) for p in read_json(records)["identities"]):
                    time.sleep(0.2)
                    continue
            return report
        time.sleep(0.2)
    raise ManagedError("IN_FLIGHT_DRAIN_TIMEOUT")


def resume(root: Path):
    host = _host(root)
    write_json(data_root(root) / "control.json", {"paused": False, "halted": False})
    try:
        running = service_running()
    except ManagedError:
        running = False
    if not running:
        if not administrator():
            raise ManagedError("ADMINISTRATOR_REQUIRED_FOR_BOOT_SERVICE")
        from .host import install_service
        plan = service_plan(root, tools_python(root / "tools"), host["service_user"], host["credential"])
        install_service(plan)
        run_checked(list(plan.start))
    return status(root)


def uninstall(root: Path):
    if not administrator():
        raise ManagedError("ADMINISTRATOR_REQUIRED_FOR_BOOT_SERVICE")
    host = _host(root)
    plan = service_plan(root, tools_python(root / "tools"), host["service_user"], host["credential"])
    if not plan.path.is_file() or plan.path.is_symlink() or plan.path.read_bytes() != plan.content:
        raise ManagedError("EXISTING_SERVICE_DEFINITION_REQUIRES_REVIEW")
    stop(root)
    # The worker is now fully exited. Stop only this exact owned OS manager.
    import sys
    from .host import TASK, UNIT, LABEL, powershell
    if sys.platform == "win32":
        # Verify the actual task, not just the saved XML, before deletion.
        from .host import install_service
        install_service(plan)
        run_checked(["schtasks.exe", "/End", "/TN", TASK])
    elif sys.platform.startswith("linux"):
        run_checked(["systemctl", "stop", UNIT])
    for command in plan.remove:
        run_checked(list(command))
    if sys.platform != "win32":
        plan.path.unlink()
        if sys.platform.startswith("linux"):
            run_checked(["systemctl", "daemon-reload"])
    write_json(data_root(root) / "install-status.json", {"stage": "boot_registration_removed_data_retained"})
    return {"uninstalled": True, "data_retained": True}
