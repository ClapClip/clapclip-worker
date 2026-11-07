"""Prepare immutable official-main releases without changing executing code."""
from __future__ import annotations

import configparser
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

from .common import ManagedError, inside, read_json, write_json
from .host import data_root

ORIGIN = "https://github.com/ClapClip/clapclip-worker.git"
PROFILE = "portable-cpu-v1"


def tools_python(prefix: Path) -> Path:
    return prefix / ("python.exe" if sys.platform == "win32" else "bin/python")


def venv_python(prefix: Path) -> Path:
    return prefix / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def executable(prefix: Path, name: str) -> str:
    folders = [prefix, prefix / "bin", prefix / "Scripts", prefix / "Library/bin", prefix / "Library/usr/bin"]
    value = shutil.which(name, path=os.pathsep.join(map(str, folders)))
    if not value or not Path(value).resolve().is_relative_to(prefix.resolve()):
        raise ManagedError("ISOLATED_TOOL_MISSING")
    return value


def tool_environment(root: Path) -> dict[str, str]:
    prefix = root / "tools"
    from ..deployment import configuration
    with configuration(None):
        env = os.environ.copy()
    env["PATH"] = os.pathsep.join(map(str, [prefix, prefix / "bin", prefix / "Scripts", prefix / "Library/bin", prefix / "Library/usr/bin"])) + os.pathsep + env.get("PATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MEDIAWORKER_LOAD_DOTENV"] = "0"
    env["PYTHONNOUSERSITE"] = "1"
    if sys.platform.startswith("linux"):
        env["LD_LIBRARY_PATH"] = str(prefix / "lib")
    return env


def command(arguments: list[str], *, cwd: Path, env: dict, timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, text=True,
                                check=False, timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        raise ManagedError("RELEASE_PREPARATION_FAILED") from None
    if result.returncode:
        # Tool stderr can contain arbitrary URL/token text. Keep it inside the
        # process boundary; public status contains only this reviewed code.
        raise ManagedError("RELEASE_VALIDATION_FAILED")
    return result


class Releases:
    def __init__(self, root: Path):
        self.root = root
        self.data = data_root(root)
        self.source = self.data / "source"
        self.base = self.data / "releases"
        self.env = tool_environment(root)
        for key in list(self.env):
            if key.startswith("GIT_") or key == "PYTHONPATH":
                del self.env[key]
        self.env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GIT_PAGER": "cat"})
        self.git = executable(root / "tools", "git")

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        hooks = self.data / "no-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        result = command([self.git, "--no-pager", "-c", f"core.hooksPath={hooks}",
                          "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                          "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                          "-c", "http.followRedirects=false", *args],
                         cwd=cwd or self.source, env=self.env, timeout=180)
        return result.stdout.strip()

    def verify_source(self) -> None:
        gitdir = self.source / ".git"
        if gitdir.is_symlink() or not gitdir.is_dir():
            raise ManagedError("UNEXPECTED_SOURCE_REPOSITORY")
        try:
            config = configparser.ConfigParser(interpolation=None)
            config.read(gitdir / "config", encoding="utf-8")
            allowed = {"core", 'remote "origin"', 'branch "main"'}
            if set(config.sections()) - allowed:
                raise ManagedError("UNEXPECTED_GIT_CONFIGURATION")
            core = set(config["core"])
            if core - {"repositoryformatversion", "filemode", "bare", "logallrefupdates", "symlinks", "ignorecase", "autocrlf", "protectntfs"}:
                raise ManagedError("UNEXPECTED_GIT_CONFIGURATION")
            remote = config['remote "origin"']
            if remote.get("url") != ORIGIN or set(remote) - {"url", "fetch"}:
                raise ManagedError("UNEXPECTED_GIT_ORIGIN")
            if remote.get("fetch") != "+refs/heads/main:refs/remotes/origin/main":
                raise ManagedError("UNEXPECTED_GIT_REFSPEC")
        except (OSError, configparser.Error, KeyError):
            raise ManagedError("UNEXPECTED_GIT_CONFIGURATION") from None
        if self._git("branch", "--show-current") != "main":
            raise ManagedError("UNEXPECTED_GIT_BRANCH")
        if self._git("status", "--porcelain", "--untracked-files=all"):
            raise ManagedError("DIRTY_SOURCE_CACHE")

    def latest(self, current: str | None = None) -> str:
        if not self.source.exists():
            self.source.parent.mkdir(parents=True, exist_ok=True)
            self._git("clone", "--single-branch", "--branch", "main", "--no-tags", ORIGIN, str(self.source), cwd=self.source.parent)
        self.verify_source()
        self._git("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
        revision = self._git("rev-parse", "refs/remotes/origin/main")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ManagedError("INVALID_OFFICIAL_REVISION")
        ancestor = current or self._git("rev-parse", "HEAD")
        self._git("merge-base", "--is-ancestor", ancestor, revision)
        return revision

    def release(self, revision: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ManagedError("INVALID_OFFICIAL_REVISION")
        return self.base / revision

    def verify_fixed(self, revision: str) -> dict:
        root = self.release(revision)
        manifest = read_json(root / "release.json")
        if manifest.get("commit") != revision or manifest.get("profile") != PROFILE:
            raise ManagedError("DEPENDENCY_PROFILE_CHANGE_REQUIRES_REVIEW")
        for name, expected in manifest.get("files", {}).items():
            path = inside(root, name)
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ManagedError("FIXED_RELEASE_MODIFIED")
        if not manifest.get("files"):
            raise ManagedError("FIXED_RELEASE_MANIFEST_INVALID")
        expected = {name for name in manifest["files"] if name.startswith("mediaworker/") and name.endswith(".py")}
        actual = {path.relative_to(root).as_posix() for path in (root / "mediaworker").rglob("*.py")}
        if actual != expected:
            raise ManagedError("FIXED_RELEASE_MODIFIED")
        return manifest

    def prepare(self, revision: str, *, profile: str = PROFILE) -> Path:
        if profile != PROFILE:
            # No CPU installation over an imported DirectML/CUDA environment.
            raise ManagedError("DEPENDENCY_PROFILE_CHANGE_REQUIRES_REVIEW")
        root = self.release(revision)
        if (root / "release.json").is_file():
            self.verify_fixed(revision)
            return root
        root.mkdir(parents=True, exist_ok=True)
        archive = root / "source.tar"
        self._git("archive", "--format=tar", f"--output={archive}", revision)
        files = {}
        total = 0
        with tarfile.open(archive) as source:
            for member in source:
                if member.isdir():
                    continue
                if not member.isfile() or member.size > 2 * 1024 * 1024:
                    raise ManagedError("UNSAFE_SOURCE_ARCHIVE")
                total += member.size
                if total > 50 * 1024 * 1024:
                    raise ManagedError("SOURCE_ARCHIVE_TOO_LARGE")
                path = inside(root, member.name)
                if member.name in {"release.json", "source.tar"}:
                    raise ManagedError("UNSAFE_SOURCE_ARCHIVE")
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream:
                    data = stream.read()
                path.write_bytes(data)
                files[member.name] = hashlib.sha256(data).hexdigest()
        python = str(tools_python(self.root / "tools"))
        command([python, "-B", "scripts/check-release.py", "--archive", str(archive)], cwd=root, env=self.env)
        environment = root / ".venv-managed"
        command([python, "-m", "venv", str(environment)], cwd=root, env=self.env)
        python = str(venv_python(environment))
        pip = [python, "-m", "pip", "--isolated", "--disable-pip-version-check", "install"]
        command([*pip, "--only-binary=:all:", "setuptools>=77", "wheel"], cwd=root, env=self.env)
        command([*pip, "--only-binary=:all:", "-r", "requirements-managed.txt"], cwd=root, env=self.env)
        command([*pip, "--no-deps", "--no-build-isolation", "-e", ".[downloader,managed]"], cwd=root, env=self.env)
        command([python, "-m", "pip", "check"], cwd=root, env=self.env)
        command([python, "-B", "-c", "from importlib.metadata import distributions; "
                 "assert not any(d.metadata['Name'].lower() in {'onnxruntime-directml','onnxruntime-gpu'} for d in distributions()); "
                 "from mediaworker.managed import PROTOCOL; assert PROTOCOL==1; "
                 "from mediaworker.deployment import platform_problem; assert platform_problem() is None; "
                 "import psutil,cv2,insightface,onnxruntime,pycognito,yt_dlp,yt_dlp_ejs"], cwd=root, env=self.env)
        command([python, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_deployment_cpu.py"], cwd=root, env=self.env)
        write_json(root / "release.json", {"commit": revision, "profile": profile, "files": files})
        archive.unlink()
        return root
