from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

RESTART_FOR_UPDATE = 75


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        creationflags=flags,
    )


def update_checkout(repo: Path, remote: str, branch: str) -> str | None:
    """Fast-forward the checkout and return the new commit, or ``None``.

    Existing local changes are never reset or overwritten. Git refuses the
    fast-forward if a changed file would conflict with the incoming revision.
    """
    fetched = _run_git(repo, "fetch", "--quiet", remote, branch)
    if fetched.returncode:
        raise RuntimeError(fetched.stderr.strip() or "git fetch failed")

    remote_ref = f"refs/remotes/{remote}/{branch}"
    local = _run_git(repo, "rev-parse", "HEAD")
    latest = _run_git(repo, "rev-parse", remote_ref)
    if local.returncode or latest.returncode:
        raise RuntimeError("could not resolve local or remote revision")
    local_sha = local.stdout.strip()
    latest_sha = latest.stdout.strip()
    if local_sha == latest_sha:
        return None

    ancestor = _run_git(repo, "merge-base", "--is-ancestor", local_sha, latest_sha)
    if ancestor.returncode:
        raise RuntimeError("local branch has diverged; update requires manual review")

    merged = _run_git(repo, "merge", "--ff-only", "--quiet", remote_ref)
    if merged.returncode:
        raise RuntimeError(merged.stderr.strip() or "fast-forward update failed")
    return latest_sha


async def watch_for_updates(cfg: Config, shutdown: asyncio.Event) -> bool:
    """Apply an update, then request a graceful process restart."""
    repo = Path(__file__).resolve().parents[1]
    while not shutdown.is_set():
        try:
            revision = await asyncio.to_thread(
                update_checkout,
                repo,
                cfg.auto_update_remote,
                cfg.auto_update_branch,
            )
        except Exception as exc:
            log.warning("automatic update check failed: %s", exc)
        else:
            if revision:
                log.info("updated worker to %.12s; finishing in-flight jobs", revision)
                shutdown.set()
                return True

        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=cfg.auto_update_interval_s
            )
        except asyncio.TimeoutError:
            pass
    return False
