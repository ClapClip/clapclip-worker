from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)


class KeepAwake:
    """Stop the machine from sleeping while the node is running, so jobs keep
    processing even when the screen is locked or no one is logged in.

    Screen lock alone never pauses a background process; system *sleep* does.
    This holds a wake lock for the lifetime of the process, independent of how
    it was launched (shell, launchd, Windows service/Task Scheduler):

      * macOS   — a `caffeinate` child process (idle + system sleep).
      * Windows — SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED).
      * else    — no-op.

    Use as a context manager: `with KeepAwake(): run()`.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._windows = sys.platform.startswith("win")
        self._darwin = sys.platform == "darwin"

    def __enter__(self) -> "KeepAwake":
        try:
            if self._darwin:
                # -i: prevent idle sleep, -s: prevent system sleep (on AC).
                self._proc = subprocess.Popen(["caffeinate", "-i", "-s"])
                log.info("keep-awake: caffeinate active")
            elif self._windows:
                import ctypes

                ES_CONTINUOUS = 0x80000000
                ES_SYSTEM_REQUIRED = 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                )
                log.info("keep-awake: SetThreadExecutionState active")
        except Exception as exc:  # keep running even if the lock can't be taken
            log.warning("keep-awake unavailable: %s", exc)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._proc is not None:
                self._proc.terminate()
                self._proc = None
            elif self._windows:
                import ctypes

                ES_CONTINUOUS = 0x80000000
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:  # pragma: no cover
            pass
