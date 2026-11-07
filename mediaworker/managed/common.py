"""Small shared persistence and diagnostic boundaries; no credential logging."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


class ManagedError(RuntimeError):
    def __init__(self, code: str):
        if not code or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for char in code):
            code = "MANAGED_OPERATION_FAILED"
        self.code = code
        super().__init__(code)


def read_json(path: Path) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            raise ManagedError("INVALID_STATE_FILE")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ManagedError("INVALID_STATE_FILE")
        return value
    except (OSError, ValueError):
        raise ManagedError("STATE_UNREADABLE") from None


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ManagedError("INVALID_STATE_FILE")
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=True, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def inside(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ManagedError("PATH_OUTSIDE_INSTALLATION")
    return path
