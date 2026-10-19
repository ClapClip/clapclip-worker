from __future__ import annotations

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from .config import Config
from .redaction import RedactingFormatter


def _configure_logging() -> None:
    logfile = Path(os.environ.get("LOG_FILE", "logs/mediaworker.log")).expanduser()
    logfile.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(logfile, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    formatter = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(prog="mediaworker", description="Local or distributed media worker.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    local = sub.add_parser("local", help="Open a standalone loopback workbench.")
    local.add_argument("--port", type=int, default=8765)
    local.add_argument("--env-file", help="Explicit local configuration; no default .env is loaded.")
    node = sub.add_parser("run", help="Connect to a private task API.")
    node.add_argument("--all-capabilities", action="store_true",
                      help="Run all configured engines; fail if any is unavailable.")
    args = parser.parse_args()
    if args.cmd == "local":
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        from .local_web import run
        run(args)
        return
    cfg = Config.from_env()
    _configure_logging()
    from .runner import run_forever
    if args.all_capabilities:
        cfg.claim_capabilities = None
        asyncio.run(run_forever(cfg, require_all_engines=True))
    else:
        asyncio.run(run_forever(cfg))


if __name__ == "__main__":
    main()
