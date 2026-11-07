"""Machine-wide ownership, OS locks, process inventory and boot-service adapters."""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from xml.sax.saxutils import escape

from .common import ManagedError, read_json, write_json
from ..single_instance import FileLock, WorkerAlreadyRunning

LABEL = "net.clapclip.worker"
TASK = "ClapClipWorker"
UNIT = "clapclip-worker.service"


def host_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", "C:/ProgramData")) / "ClapClipWorker"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClapClipWorker")
    return Path("/var/lib/clapclip-worker")


def data_root(root: Path) -> Path:
    record = root / "host.json"
    if sys.platform.startswith("linux") and record.is_file() and read_json(record).get("service_user") == "dynamic":
        # Keep systemd's StateDirectory migration away from the immutable host
        # root, launcher, tools and machine lock. This is the one exact OS path.
        path = Path("/var/lib/clapclip-worker-runtime")
        if path.is_symlink():
            expected = Path("/var/lib/private/clapclip-worker-runtime")
            if path.resolve() != expected or path.lstat().st_uid != 0:
                raise ManagedError("UNEXPECTED_SYSTEMD_STATE_LINK")
        return path
    return root / "data"


class HostLock:
    """One namespace across source copies, node configs and user sessions."""
    def __init__(self, root: Path):
        self.root, self.lock, self.handle = root, None, None

    def __enter__(self):
        if sys.platform != "win32":
            try:
                self.lock = FileLock(self.root / "host.lock").acquire()
            except WorkerAlreadyRunning:
                raise ManagedError("HOST_ALREADY_OWNED") from None
            return self
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # The installer/service run with normal OS-authorized credentials. An
        # inaccessible existing mutex is a conflict, never permission to fork.
        handle = kernel.CreateMutexW(None, False, "Global\\ClapClipWorkerHost-v1")
        if not handle:
            raise ManagedError("HOST_IDENTITY_UNVERIFIABLE")
        if kernel.WaitForSingleObject(handle, 0) not in {0, 0x80}:
            kernel.CloseHandle(handle)
            raise ManagedError("HOST_ALREADY_OWNED")
        self.handle, self.kernel = handle, kernel
        return self

    def __exit__(self, *_args):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.lock:
            self.lock.release()


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    created: float
    image: str
    role: str


def inventory(*, exclude: set[int] | None = None) -> list[Process]:
    import psutil
    exclude = set(exclude or ())
    found = []
    try:
        for process in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "create_time"], ad_value=None):
            item = process.info
            if item["pid"] in exclude:
                continue
            name = (item["name"] or "").lower()
            words = item["cmdline"]
            python = "python" in name or name == "clapclipwoker.exe"
            if not words:
                if python:
                    raise ManagedError("HOST_PROCESS_IDENTITY_UNVERIFIABLE")
                continue
            command = " ".join(words)
            role = None
            if re.search(r"(?:^|\s)-m\s+mediaworker\.supervisor\b", command):
                role = "legacy_supervisor"
            elif re.search(r"(?:^|\s)-m\s+mediaworker\s+(?:run|local|download-worker)\b", command):
                role = "legacy_worker"
            elif re.search(r"(?:^|\s)-m\s+mediaworker\.managed\s+(?:agent|worker)\b", command):
                role = "managed"
            elif any(Path(word).name == "clapclip-agent.py" for word in words):
                role = "launcher"
            elif name == "clapclipwoker.exe" or any(Path(word).name.lower() in {"clapclipwoker.exe", "clapclipwoker"} for word in words):
                role = "unknown_worker"
            if role:
                if not item["exe"] or not item["create_time"]:
                    raise ManagedError("HOST_PROCESS_IDENTITY_UNVERIFIABLE")
                found.append(Process(int(item["pid"]), int(item["ppid"]),
                                     float(item["create_time"]), item["exe"], role))
    except (psutil.AccessDenied, psutil.Error):
        raise ManagedError("HOST_PROCESS_INVENTORY_FAILED") from None
    return found


def require_no_other_worker(*, permitted: set[int] | None = None) -> None:
    found = inventory(exclude={os.getpid(), *(permitted or ())})
    if found:
        # Only sanitized identities leave this boundary, never other argv.
        raise ManagedError("EXISTING_WORKER_REQUIRES_REUSE_OR_MIGRATION")


def administrator() -> bool:
    if sys.platform == "win32":
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def service_user() -> str:
    if sys.platform == "win32":
        return "S-1-5-19"  # LocalService, not Administrator/SYSTEM.
    import pwd
    uid = int(os.environ.get("SUDO_UID", os.getuid()))
    if uid == 0:
        if sys.platform == "darwin":
            raise ManagedError("RUN_INSTALLER_FROM_REGULAR_MAC_USER")
        return "dynamic"
    pwd.getpwuid(uid)
    return str(uid)


def powershell() -> str:
    return str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")


def run_checked(arguments: list[str], *, input: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(arguments, input=input, capture_output=True, check=False, timeout=180,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        raise ManagedError("OS_SERVICE_COMMAND_FAILED") from None
    if result.returncode:
        raise ManagedError("OS_SERVICE_COMMAND_FAILED")
    return result


def protect_new_root(root: Path, user: str) -> None:
    """Only this newly owned application's directories; never global ACL policy."""
    if root.resolve() != host_root().resolve() or any(path.is_symlink() for path in (root, *root.parents)):
        raise ManagedError("UNEXPECTED_HOST_DIRECTORY")
    marker = root / "bootstrap-owner.txt"
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != "clapclip-bootstrap-v1":
        raise ManagedError("HOST_DIRECTORY_NOT_OWNED")
    root.mkdir(parents=True, exist_ok=True)
    data = data_root(root)
    data.mkdir(exist_ok=True)
    if sys.platform == "win32":
        run_checked(["icacls.exe", str(root), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-19:(OI)(CI)RX"])
        run_checked(["icacls.exe", str(data), "/grant:r", "*S-1-5-19:(OI)(CI)M"])
    else:
        root.chmod(0o755)
        data.chmod(0o700)
        if user != "dynamic":
            import pwd
            account = pwd.getpwuid(int(user))
            os.chown(data, account.pw_uid, account.pw_gid)
        lock = root / "host.lock"
        if lock.is_symlink():
            raise ManagedError("UNSAFE_HOST_LOCK")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o666)
        os.close(descriptor)
        lock.chmod(0o666)  # Root-owned parent prevents replacement; OS lock owns uniqueness.


def handoff_data(root: Path, user: str) -> None:
    if sys.platform == "win32" or user == "dynamic":
        return
    import pwd
    account = pwd.getpwuid(int(user))
    data = data_root(root)
    for parent, directories, files in os.walk(data, followlinks=False):
        for path in [Path(parent), *(Path(parent) / name for name in directories + files)]:
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)


def _unit_arg(value: str) -> str:
    if any(char in value for char in "\n\r\0"):
        raise ManagedError("INVALID_SERVICE_PATH")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


@dataclass(frozen=True)
class ServicePlan:
    path: Path
    content: bytes
    enable: tuple[tuple[str, ...], ...]
    start: tuple[str, ...]
    remove: tuple[tuple[str, ...], ...]


def service_plan(root: Path, interpreter: Path, user: str, credential: dict | None = None) -> ServicePlan:
    launcher = root / "clapclip-agent.py"
    if sys.platform == "darwin":
        import pwd
        account = pwd.getpwuid(int(user)).pw_name
        content = plistlib.dumps({"Label": LABEL, "UserName": account,
            "ProgramArguments": [str(interpreter), "-B", str(launcher)],
            "WorkingDirectory": str(root), "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 60,
            "ExitTimeOut": 0, "ProcessType": "Background",
            "StandardOutPath": str(root / "data/agent-console.log"),
            "StandardErrorPath": str(root / "data/agent-console.log")})
        path = Path("/Library/LaunchDaemons") / f"{LABEL}.plist"
        return ServicePlan(path, content, (("/bin/launchctl", "bootstrap", "system", str(path)),),
                           ("/bin/launchctl", "kickstart", f"system/{LABEL}"),
                           (("/bin/launchctl", "bootout", f"system/{LABEL}"),))
    if sys.platform == "win32":
        # XML contains only public paths/service identity, never a user password.
        command = escape(str(interpreter.with_name("pythonw.exe")))
        args = escape(subprocess.list2cmdline(["-B", str(launcher)]))
        content = (f'<?xml version="1.0" encoding="UTF-16"?><Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            f'<RegistrationInfo><Description>{LABEL}:managed-v1</Description></RegistrationInfo>'
            '<Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger></Triggers>'
            '<Principals><Principal id="Service"><UserId>S-1-5-19</UserId><LogonType>ServiceAccount</LogonType></Principal></Principals>'
            '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
            '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable>'
            '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden>'
            '<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>'
            f'<Actions Context="Service"><Exec><Command>{command}</Command><Arguments>{args}</Arguments>'
            f'<WorkingDirectory>{escape(str(root))}</WorkingDirectory></Exec></Actions></Task>').encode("utf-16")
        path = root / "startup-task.xml"
        return ServicePlan(path, content, (("schtasks.exe", "/Create", "/TN", TASK, "/XML", str(path)),),
                           ("schtasks.exe", "/Run", "/TN", TASK), (("schtasks.exe", "/Delete", "/TN", TASK, "/F"),))
    path = Path("/etc/systemd/system") / UNIT
    identity = "DynamicUser=yes\nStateDirectory=clapclip-worker-runtime\nStateDirectoryMode=0700\n" if user == "dynamic" else f"User={int(user)}\n"
    content = ("[Unit]\nDescription=ClapClip managed Worker\nWants=network-online.target\nAfter=network-online.target\n"
               "StartLimitIntervalSec=900\nStartLimitBurst=4\n[Service]\nType=simple\n" + identity +
               f"WorkingDirectory={root}\nExecStart={_unit_arg(str(interpreter))} -B {_unit_arg(str(launcher))}\n"
               "Restart=on-failure\nRestartSec=60\nRestartPreventExitStatus=78\nTimeoutStopSec=infinity\nKillMode=mixed\n"
               "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\n[Install]\nWantedBy=multi-user.target\n").encode()
    if credential:
        from .credentials import systemd_directive
        content = content.replace(b"[Install]", systemd_directive(root, credential).encode() + b"[Install]")
    return ServicePlan(path, content, (("systemctl", "daemon-reload"), ("systemctl", "enable", UNIT)),
                       ("systemctl", "start", UNIT), (("systemctl", "disable", UNIT), ("systemctl", "daemon-reload")))


def install_service(plan: ServicePlan) -> None:
    if not administrator():
        raise ManagedError("ADMINISTRATOR_REQUIRED_FOR_BOOT_SERVICE")
    if plan.path.is_symlink():
        raise ManagedError("EXISTING_SERVICE_DEFINITION_REQUIRES_REVIEW")
    if plan.path.exists():
        if plan.path.read_bytes() != plan.content:
            raise ManagedError("EXISTING_SERVICE_DEFINITION_REQUIRES_REVIEW")
    if sys.platform == "win32":
        script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                  "$t=@(Get-ScheduledTask -ErrorAction Stop | "
                  f"Where-Object TaskName -eq '{TASK}'); if($t.Count){{Export-ScheduledTask -TaskName '{TASK}'}}")
        probe = run_checked([powershell(), "-NoProfile", "-NonInteractive", "-Command", script])
        if probe.stdout.strip():
            from xml.etree import ElementTree
            try:
                tree = ElementTree.fromstring(probe.stdout.decode("utf-8-sig", errors="strict"))
                expected = ElementTree.fromstring(plan.content.decode("utf-16"))
                ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                fields = ("t:RegistrationInfo/t:Description", "t:Principals/t:Principal/t:UserId",
                          "t:Actions/t:Exec/t:Command", "t:Actions/t:Exec/t:Arguments", "t:Actions/t:Exec/t:WorkingDirectory")
                if any(tree.findtext(field, namespaces=ns) != expected.findtext(field, namespaces=ns) for field in fields):
                    raise ManagedError("EXISTING_TASK_NOT_OWNED")
                if tree.find("t:Triggers/t:BootTrigger", ns) is None:
                    raise ManagedError("EXISTING_TASK_NOT_OWNED")
                if (len(tree.findall("t:Triggers/*", ns)) != 1 or len(tree.findall("t:Actions/*", ns)) != 1
                        or tree.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns) != "ServiceAccount"
                        or tree.findtext("t:Triggers/t:BootTrigger/t:Enabled", "true", ns) != "true"
                        or tree.findtext("t:Settings/t:Enabled", "true", ns) != "true"):
                    raise ManagedError("EXISTING_TASK_NOT_OWNED")
            except (ValueError, UnicodeError, ElementTree.ParseError):
                raise ManagedError("EXISTING_TASK_NOT_OWNED") from None
            return
    elif sys.platform == "darwin" and plan.path.exists():
        probe = subprocess.run(["/bin/launchctl", "print", f"system/{LABEL}"], capture_output=True, check=False)
        if probe.returncode == 0:
            return
        if probe.returncode not in {3, 113}:
            raise ManagedError("OS_SERVICE_IDENTITY_UNVERIFIABLE")
    plan.path.write_bytes(plan.content)
    if os.name != "nt":
        plan.path.chmod(0o644)
    try:
        for command in plan.enable:
            run_checked(list(command))
    except Exception:
        # Do not claim boot registration completed after an OS failure.
        raise ManagedError("BOOT_REGISTRATION_FAILED") from None


def service_running() -> bool:
    if sys.platform == "win32":
        result = run_checked([powershell(), "-NoProfile", "-NonInteractive", "-Command",
                              f"(Get-ScheduledTask -TaskName '{TASK}' -ErrorAction Stop).State.ToString()"])
        return result.stdout.decode(errors="replace").strip() == "Running"
    if sys.platform == "darwin":
        result = run_checked(["/bin/launchctl", "print", f"system/{LABEL}"])
        return b"state = running" in result.stdout
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0


def service_registered(plan: ServicePlan) -> bool:
    """Read actual persistent OS registration; a local XML/plist is insufficient."""
    if plan.path.is_symlink() or not plan.path.is_file() or plan.path.read_bytes() != plan.content:
        return False
    try:
        if sys.platform == "win32":
            from xml.etree import ElementTree
            result = run_checked([powershell(), "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                f"Export-ScheduledTask -TaskName '{TASK}' -ErrorAction Stop"])
            actual = ElementTree.fromstring(result.stdout.decode("utf-8-sig"))
            expected = ElementTree.fromstring(plan.content.decode("utf-16"))
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            fields = ("t:RegistrationInfo/t:Description", "t:Principals/t:Principal/t:UserId",
                      "t:Principals/t:Principal/t:LogonType", "t:Actions/t:Exec/t:Command",
                      "t:Actions/t:Exec/t:Arguments", "t:Actions/t:Exec/t:WorkingDirectory")
            return (all(actual.findtext(p, namespaces=ns) == expected.findtext(p, namespaces=ns) for p in fields)
                    and len(actual.findall("t:Actions/*", ns)) == 1
                    and len(actual.findall("t:Triggers/*", ns)) == 1
                    and actual.find("t:Triggers/t:BootTrigger", ns) is not None
                    and actual.findtext("t:Settings/t:Enabled", "true", ns) == "true"
                    and actual.findtext("t:Triggers/t:BootTrigger/t:Enabled", "true", ns) == "true")
        if sys.platform == "darwin":
            run_checked(["/bin/launchctl", "print", f"system/{LABEL}"])
            disabled = run_checked(["/bin/launchctl", "print-disabled", "system"])
            return not re.search(rb'"net\.clapclip\.worker"\s*=>\s*true', disabled.stdout)
        result = run_checked(["systemctl", "show", UNIT, "--property=LoadState", "--property=FragmentPath", "--property=UnitFileState"])
        value = dict(row.split("=", 1) for row in result.stdout.decode().splitlines() if "=" in row)
        return (value.get("LoadState") == "loaded" and value.get("UnitFileState") == "enabled"
                and Path(value.get("FragmentPath", "")).resolve() == plan.path.resolve())
    except (ManagedError, OSError, ValueError, UnicodeError):
        return False
