"""Boot-available credentials; never put a password in argv, env or state JSON."""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .common import ManagedError

NAME = "worker-login"


@dataclass(repr=False)
class Login:
    username: str
    password: str

    def encode(self) -> bytes:
        if not self.username or not self.password:
            raise ManagedError("LOGIN_REQUIRED")
        return json.dumps({"username": self.username, "password": self.password}, ensure_ascii=True).encode()

    @classmethod
    def decode(cls, data: bytes):
        try:
            value = json.loads(data)
            user, password = value["username"], value["password"]
            if not isinstance(user, str) or not isinstance(password, str) or not user or not password:
                raise ValueError
            return cls(user, password)
        except (ValueError, TypeError, KeyError):
            raise ManagedError("CREDENTIAL_UNREADABLE") from None


def _private_write(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ManagedError("UNSAFE_CREDENTIAL_PATH")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    if decrypt:
        operation = crypt.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(Blob), wintypes.LPVOID, wintypes.LPVOID,
                              wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(Blob)]
        okay = operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        operation = crypt.CryptProtectData
        operation.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, wintypes.LPVOID,
                              wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(Blob)]
        okay = operation(ctypes.byref(source), "ClapClip Worker", None, None, None,
                         1 | 4, ctypes.byref(output))  # UI forbidden + machine scope.
    if not okay:
        raise ManagedError("OS_CREDENTIAL_PROTECTION_FAILED")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def store(root: Path, login: Login) -> dict:
    data = login.encode()
    if sys.platform == "win32":
        path = root / "data/credential.dpapi"
        _private_write(path, _dpapi(data))
        return {"kind": "windows-dpapi-machine", "path": "data/credential.dpapi"}
    if sys.platform.startswith("linux"):
        binary = shutil.which("systemd-creds")
        if binary:
            path = root / "credential.enc"
            result = subprocess.run([binary, "encrypt", "--with-key=host", f"--name={NAME}", "-", "-"],
                                    input=data, capture_output=True, check=False, timeout=30)
            if result.returncode:
                raise ManagedError("OS_CREDENTIAL_PROTECTION_FAILED")
            _private_write(path, result.stdout)
            return {"kind": "systemd-encrypted", "path": "credential.enc"}
        path = root / "credential.private"
        _private_write(path, data)
        return {"kind": "systemd-private-file", "path": "credential.private",
                "notice": "Encrypted systemd store unavailable; root-only 0600 source is delivered via LoadCredential."}
    path = root / "data/credential.private"
    _private_write(path, data)
    return {"kind": "macos-private-file", "path": "data/credential.private",
            "notice": "Boot LaunchDaemon cannot rely on the locked login keychain; credentials use a 0600 file in a 0700 private data directory."}


def load(root: Path, reference: dict) -> Login:
    from .common import inside
    kind = reference.get("kind")
    if kind not in {"windows-dpapi-machine", "systemd-encrypted", "systemd-private-file", "macos-private-file"}:
        raise ManagedError("OS_CREDENTIAL_STORE_UNAVAILABLE")
    path = inside(root, reference.get("path", ""))
    try:
        if kind == "windows-dpapi-machine":
            return Login.decode(_dpapi(path.read_bytes(), decrypt=True))
        if kind in {"systemd-encrypted", "systemd-private-file"}:
            runtime = os.environ.get("CREDENTIALS_DIRECTORY")
            if runtime:
                return Login.decode((Path(runtime) / NAME).read_bytes())
            if os.geteuid() != 0:
                raise ManagedError("OS_CREDENTIAL_STORE_UNAVAILABLE")
            if kind == "systemd-encrypted":
                result = subprocess.run(["systemd-creds", "decrypt", f"--name={NAME}", str(path), "-"],
                                        capture_output=True, check=False, timeout=30)
                if result.returncode:
                    raise ManagedError("OS_CREDENTIAL_STORE_UNAVAILABLE")
                return Login.decode(result.stdout)
        if os.name != "nt" and (path.is_symlink() or path.stat().st_mode & 0o077):
            raise ManagedError("CREDENTIAL_FILE_PERMISSIONS_UNSAFE")
        return Login.decode(path.read_bytes())
    except OSError:
        raise ManagedError("OS_CREDENTIAL_STORE_UNAVAILABLE") from None


def systemd_directive(root: Path, reference: dict) -> str:
    from .common import inside
    name = "LoadCredentialEncrypted" if reference["kind"] == "systemd-encrypted" else "LoadCredential"
    path = str(inside(root, reference["path"]))
    if any(char in path for char in "\n\r\0"):
        raise ManagedError("UNSAFE_CREDENTIAL_PATH")
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'{name}={NAME}:"{escaped}"\n'
