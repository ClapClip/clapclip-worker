"""Verified process handles, including the Windows venv launcher/child pair."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

from .common import ManagedError
from .host import Process
from .releases import tools_python, venv_python


def identity(pid: int) -> Process:
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            return Process(p.pid, p.ppid(), p.create_time(), p.exe(), "owned")
    except psutil.NoSuchProcess:
        raise ManagedError("OWNED_PROCESS_EXITED") from None
    except psutil.Error:
        raise ManagedError("OWNED_PROCESS_IDENTITY_UNVERIFIABLE") from None


def same_process(record: Process) -> bool:
    try:
        current = identity(record.pid)
        return current.created == record.created and Path(current.image).resolve() == Path(record.image).resolve()
    except ManagedError as error:
        if error.code == "OWNED_PROCESS_EXITED":
            return False
        # Access failure is not evidence of exit. Managers retain ownership;
        # runtimes treat the unverifiable owner as a request to drain.
        raise


class Child:
    def __init__(self, root: Path, release: Path, session: str, revision: str, env: dict, *, launch=subprocess.Popen):
        self.root, self.release, self.session, self.revision = root, release, session, revision
        self.arguments = [str(venv_python(release / ".venv-managed")), "-B", "-m", "mediaworker.managed", "worker",
                          "--root", str(root), "--revision", revision, "--session", session]
        self.popen = launch(self.arguments, cwd=release, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        self.launcher = identity(self.popen.pid)
        self.known = {self.launcher.pid: self.launcher}
        if Path(self.launcher.image).resolve() not in self.images():
            raise ManagedError("UNEXPECTED_CHILD_IMAGE")

    def images(self) -> set[Path]:
        return {venv_python(self.release / ".venv-managed").resolve(), tools_python(self.root / "tools").resolve()}

    def refresh(self) -> dict[int, Process]:
        # No blanket "all descendants" exception: only exact worker argv,
        # expected interpreter, birth time, and an authenticated parent edge.
        for parent in list(self.known.values()):
            if not same_process(parent):
                continue
            try:
                children = psutil.Process(parent.pid).children(recursive=False)
                for p in children:
                    record = identity(p.pid)
                    if (record.parent == parent.pid and record.created >= parent.created
                            and Path(record.image).resolve() in self.images()
                            and p.cmdline()[1:] == self.arguments[1:]):
                        self.known[record.pid] = record
            except psutil.Error:
                raise ManagedError("OWNED_PROCESS_IDENTITY_UNVERIFIABLE") from None
        return {pid: p for pid, p in self.known.items() if same_process(p)}

    def ready_identity(self, pid: int) -> bool:
        return pid in self.refresh()

    def alive(self) -> bool:
        self.popen.poll()  # Reap POSIX zombies before identity probing.
        return bool(self.refresh())

    def terminate_gated(self) -> None:
        # Only called before admission, never while claims are enabled.
        for record in reversed(list(self.refresh().values())):
            try:
                p = psutil.Process(record.pid)
                with p.oneshot():
                    if p.create_time() != record.created or Path(p.exe()).resolve() != Path(record.image).resolve():
                        raise ManagedError("OWNED_PROCESS_IDENTITY_CHANGED")
                p.terminate()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                raise ManagedError("OWNED_PROCESS_STOP_FAILED") from None


def manager_family(root: Path) -> set[int]:
    """Authenticate the complete stable-launcher / venv-shim / agent chain."""
    own = identity(os.getpid())
    allowed = {own.pid}
    images = {Path(sys.executable).resolve(), Path(getattr(sys, "_base_executable", sys.executable)).resolve(),
              tools_python(root / "tools").resolve(), venv_python(Path(sys.prefix)).resolve()}
    if sys.platform == "win32":
        images.update({tools_python(root / "tools").with_name("pythonw.exe").resolve(),
                       Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe").resolve()})
    try:
        child = own
        child_args = psutil.Process(own.pid).cmdline()
        for _ in range(3):
            parent = psutil.Process(child.parent)
            record = identity(parent.pid)
            args = parent.cmdline()
            if Path(record.image).resolve() not in images or record.created > child.created:
                break
            stable = args[1:] == ["-B", str(root / "clapclip-agent.py")]
            shim = args[1:] == child_args[1:]
            if not (stable or shim):
                break
            allowed.add(record.pid)
            if stable:
                break
            child, child_args = record, args
    except psutil.NoSuchProcess:
        pass
    except psutil.Error:
        raise ManagedError("MANAGER_PARENT_IDENTITY_UNVERIFIABLE") from None
    return allowed
