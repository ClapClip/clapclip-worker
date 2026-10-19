"""Windows GUI-Python supervisor: adopt live runtimes without console signals."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import threading

from .single_instance import FileLock, WorkerAlreadyRunning
from .redaction import RedactingFormatter

log = logging.getLogger("mediaworker.supervisor")


def process_roots(rows: list[dict]) -> list[dict]:
    """Count a launcher and its Python child as one runtime, not two."""
    by_pid = {int(row["ProcessId"]): row for row in rows}
    if any(pid <= 0 for pid in by_pid) or len(by_pid) != len(rows):
        raise ValueError("invalid process inventory")
    roots = []
    for row in rows:
        parent = int(row["ParentProcessId"])
        seen = {int(row["ProcessId"])}
        has_parent = False
        while parent in by_pid:
            if parent in seen:
                raise ValueError("cyclic process inventory")
            seen.add(parent)
            has_parent = True
            parent = int(by_pid[parent]["ParentProcessId"])
        if not has_parent:
            roots.append(row)
    return roots


def snapshot(mode: str, shell: Path) -> list[dict]:
    # Only fixed, validated mode values enter this command. No .env is read.
    if mode not in {"run", "download-worker"}:
        raise ValueError("invalid runtime mode")
    command = (
        "$ErrorActionPreference='Stop'; "
        "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='ClapClipWoker.exe'\" "
        "| Where-Object { $_.CommandLine -match '(?i)(?:^|\\s)-m\\s+mediaworker\\s+"
        + mode + "(?:\\s|$)' } "
        "| Select-Object ProcessId,ParentProcessId,ExecutablePath) | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    data = json.loads(result.stdout) if result.stdout.strip() else []
    return data if isinstance(data, list) else [data]


def wait_for_existing(row: dict, stop: threading.Event) -> None:
    """Wait on a verified OS handle; never stop an adopted Worker."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, int(row["ProcessId"]))
    if not handle:
        raise OSError(ctypes.get_last_error(), "cannot inspect existing runtime")
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "cannot verify runtime image")
        if not row.get("ExecutablePath") or os.path.normcase(name.value) != os.path.normcase(row["ExecutablePath"]):
            raise ValueError("process identity changed")
        while not stop.is_set():
            status = kernel.WaitForSingleObject(handle, 1000)
            if status == 0:
                log.info("existing runtime exited pid=%d", row["ProcessId"])
                return
            if status != 258:
                raise OSError(ctypes.get_last_error(), "cannot wait on runtime")
    finally:
        kernel.CloseHandle(handle)


def supervise(mode: str, worker: Path, shell: Path, root: Path, stop: threading.Event,
              *, all_capabilities: bool = False) -> None:
    if mode != "run":
        raise ValueError("only unified run mode can be supervised")
    while not stop.is_set():
        try:
            rows = snapshot(mode, shell)
            roots = process_roots(rows)
            if roots:
                # Even an orphan or a foreign runtime blocks a new launch.
                # Re-enumerate after it exits; never claim ownership or kill it.
                log.info("adopting existing runtime mode=%s roots=%d pid=%d", mode, len(roots), roots[0]["ProcessId"])
                wait_for_existing(roots[0], stop)
                continue
            env = os.environ.copy()
            command = [str(worker), "-m", "mediaworker", mode]
            if all_capabilities:
                command.append("--all-capabilities")
            child = subprocess.Popen(
                command, cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log.info("started runtime mode=%s pid=%d", mode, child.pid)
            while not stop.wait(1) and child.poll() is None:
                pass
            if stop.is_set():
                return  # Keep an in-flight child alive for the next supervisor.
            log.warning("runtime exited mode=%s pid=%d code=%s", mode, child.pid, child.returncode)
        except Exception as error:
            # Inventory/identity failures fail closed; do not risk a duplicate.
            log.error("supervisor check failed mode=%s type=%s", mode, type(error).__name__)
        stop.wait(5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Keep the old name only for inventory, so migration checks can detect an
    # orphaned dedicated process. Startup is restricted to unified run below.
    parser.add_argument("--mode", choices=["run", "download-worker"], required=True)
    parser.add_argument("--worker-exe", type=Path, required=True)
    parser.add_argument("--shell", type=Path, required=True)
    parser.add_argument("--check-existing", action="store_true")
    parser.add_argument("--all-capabilities", action="store_true")
    args = parser.parse_args()
    if args.mode != "run" and not args.check_existing:
        parser.error("dedicated download mode is retired; use run --all-capabilities")
    root = Path(__file__).resolve().parents[1]
    worker = args.worker_exe.resolve(strict=True)
    shell = args.shell.resolve(strict=True)
    if not worker.is_relative_to(root):
        parser.error("Worker runtime must be inside this checkout")
    if args.check_existing:
        roots = process_roots(snapshot(args.mode, shell))
        print(json.dumps({"mode": args.mode, "runtime_count": len(roots), "pids": [r["ProcessId"] for r in roots]}))
        raise SystemExit(0 if len(roots) == 1 else 1)
    log_path = root / "logs" / f"supervisor-{args.mode}.log"
    log_path.parent.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(log_path, maxBytes=256000, backupCount=2, encoding="utf-8")])
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        with FileLock(Path.home() / ".mediaworker" / f"supervisor-{args.mode}.lock"):
            log.info("supervisor online mode=%s pid=%d", args.mode, os.getpid())
            supervise(args.mode, worker, shell, root, stop, all_capabilities=args.all_capabilities)
    except WorkerAlreadyRunning:
        log.info("another supervisor already owns mode=%s; exiting", args.mode)


if __name__ == "__main__":
    main()
