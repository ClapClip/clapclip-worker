"""Managed installation CLI: diagnostic errors contain only reviewed codes."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import re

from .common import ManagedError, read_json, write_json
from .host import data_root, host_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "agent", "worker", "inspect", "status", "version", "stop", "resume", "uninstall"])
    parser.add_argument("--root", type=Path, default=host_root(), help=argparse.SUPPRESS)
    parser.add_argument("--session", help=argparse.SUPPRESS)
    parser.add_argument("--revision", help=argparse.SUPPRESS)
    parser.add_argument("--configuration", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "worker":
            if not re.fullmatch(r"[0-9a-f]{32}", args.session or "") or not re.fullmatch(r"[0-9a-f]{40}", args.revision or ""):
                raise ManagedError("INVALID_RUNTIME_SESSION")
            from .runtime import worker
            asyncio.run(worker(args.root, args.revision, args.session))
            return 0
        if args.command == "agent":
            from .agent import Agent
            return Agent(args.root).run()
        if args.command == "inspect":
            # Native model libraries can write directly to stdout. The installer
            # captures this subprocess; only its return code establishes ready.
            from .runtime import inspect
            value = inspect(args.root, read_json(args.configuration))
        elif args.command == "install":
            from .setup import install
            value = install(args.root)
        else:
            from . import control
            value = control.status(args.root) if args.command in {"status", "version"} else getattr(control, args.command)(args.root)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (ManagedError, OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit) as error:
        code = error.code if isinstance(error, ManagedError) else "MANAGED_OPERATION_FAILED"
        if args.command in {"worker", "agent"}:
            directory = data_root(args.root)
            if args.command == "worker" and args.session and re.fullmatch(r"[0-9a-f]{32}", args.session):
                directory = directory / "sessions" / args.session
            write_json(directory / "failure.json", {"code": code})
        print(code)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
