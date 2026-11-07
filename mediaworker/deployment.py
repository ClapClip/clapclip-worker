"""Portable setup. No default private dotenv, model download, service or claim."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from importlib import import_module
import io
import json
import logging
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import sysconfig
import uuid
import venv
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENV_NAME = ".venv-portable"
MARKER = "clapclip-portable.json"
IMPORTS = ("httpx", "pydantic", "dotenv", "PIL.Image", "numpy", "cv2",
           "onnxruntime", "insightface", "pycognito", "yt_dlp", "yt_dlp_ejs")
CAPABILITIES = ("faceswap", "cutout", "enhance", "download")


def platform_problem() -> str | None:
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 13):
        return "Use ordinary CPython 3.13 (not 3.14/3.15, PyPy or free-threaded Python). https://www.python.org/downloads/"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        return "Free-threaded Python has different wheel tags. Use ordinary CPython 3.13."
    arch = platform.machine().lower()
    if sys.platform == "win32":
        if arch not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
            return "Portable Windows wheels require x64 Python; Windows ARM64/32-bit is not covered."
    elif sys.platform == "darwin":
        translated = subprocess.run(["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
                                    capture_output=True, text=True, check=False)
        if translated.stdout.strip() == "1":
            return "Rosetta Python detected. Reopen Terminal without Rosetta and run arm64 or universal2 Python 3.13 natively as arm64."
        if arch != "arm64":
            return "Intel Macs are not supported. macOS deployment requires an Apple Silicon M-series Mac with native arm64 Python."
        if tuple(map(int, platform.mac_ver()[0].split(".")[:1])) < (13,):
            return "Apple Silicon M-series deployment requires macOS 13+ and native arm64 Python 3.13."
    elif sys.platform.startswith("linux"):
        libc, version = platform.libc_ver()
        if arch not in {"x86_64", "aarch64", "arm64"} or libc != "glibc" or tuple(map(int, version.split(".")[:2])) < (2, 27):
            return "Use x64/ARM64 glibc Linux >=2.27 (e.g. Ubuntu 22.04+); Alpine/musl is not covered."
    else:
        return "No portable wheel profile for this OS. See docs/portable-setup.md."
    return None


def python_in(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def system_help() -> str:
    if sys.platform == "darwin":
        return ("Apple Silicon M-series only: install Homebrew from https://brew.sh, then brew install python@3.13 ffmpeg node. "
                "Alternatively use arm64/universal2 Python 3.13 from python.org, running natively as arm64, "
                "Node.js 22+ from nodejs.org and FFmpeg/FFprobe from ffmpeg.org/download.html. Intel/Rosetta is not supported.")
    if sys.platform == "win32":
        return ("Install Python 3.13 x64 from python.org and Node.js 22+ from nodejs.org; "
                "install FFmpeg with FFprobe and libx264/libvpx-vp9/aac/libopus/libmp3lame from ffmpeg.org/download.html. "
                "Add their bin folders to PATH and open a new terminal.")
    return ("Ubuntu/Debian: sudo apt-get update && sudo apt-get install ffmpeg libgl1 libglib2.0-0 python3.13-venv; "
            "use your distribution's supported Python 3.13 package or python.org, and Node.js 22+ from nodejs.org/en/download. "
            "An older distro Node package may be too old. No system packages are installed automatically.")


def write_example(path: Path, node: str, *, local: bool) -> None:
    state = f"~/.mediaworker/nodes/{node}"
    lines = ["# Generated portable configuration; keep private. Existing files are never replaced.",
             f"NODE_ID={node}", f"NODE_RUNTIME_DIR={state}", f"NODE_TOKEN_FILE={state}/node-token.json",
             f"WORK_DIR={state}/work", f"LOG_FILE={state}/logs/worker.log",
             "CAPABILITIES=faceswap,cutout,download", "CONCURRENCY=3", "WORKER_AUTO_UPDATE=false",
             "FACESWAP_MODEL_AUTO_DOWNLOAD=false", "CUTOUT_MODEL_AUTO_DOWNLOAD=false"]
    if not local:
        lines += ["# Central Web-ClapClip backend manages all website queues.", "API_BASE_URL=https://www.clapclip.net",
                  "WORKER_COGNITO_USERNAME=", "WORKER_COGNITO_PASSWORD=", "WORKER_COGNITO_USER_POOL_ID=",
                  "WORKER_COGNITO_CLIENT_ID=", "WORKER_COGNITO_REGION=ap-northeast-1", "NODE_TOKEN="]
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def install(root: Path, directory: Path, *, repair: bool = False) -> int:
    problem = platform_problem()
    if problem:
        raise SystemExit(problem)
    if any(part.is_symlink() for part in (directory, *directory.parents)) or directory.resolve() == root.resolve():
        raise SystemExit("Choose a dedicated virtual environment directory, not the source directory or a symlink.")
    marker = directory / MARKER
    if marker.is_symlink():
        raise SystemExit("Environment ownership marker must not be a symlink. Use a new --venv directory.")
    if directory.exists() and not marker.is_file():
        raise SystemExit("Existing environment is not managed by this installer. Preserve it; use --venv with a new directory.")
    if marker.is_file():
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise SystemExit("Environment ownership marker is unreadable. Preserve it and choose a new --venv directory.") from None
        if info != {"profile": "portable-cp313", "source": str(root.resolve())}:
            raise SystemExit("Environment belongs to another source/profile. Use a new --venv directory.")
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".setup-lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise SystemExit("Installer already running, or interrupted. Check before removing the environment's .setup-lock directory.") from None
    try:
        marker.write_text(json.dumps({"profile": "portable-cp313", "source": str(root.resolve())}), encoding="utf-8")
        if not python_in(directory).is_file():
            venv.EnvBuilder(with_pip=True).create(directory)
        interpreter = str(python_in(directory))
        # Never let this path replace a DirectML/CUDA runtime or compile native dependencies.
        check = subprocess.run([interpreter, "-c", "import sys; from importlib.metadata import distributions; "
                                "assert sys.version_info[:2]==(3,13); "
                                "assert not any(d.metadata['Name'].lower() in "
                                "{'onnxruntime-directml','onnxruntime-gpu'} for d in distributions())"],
                               capture_output=True, check=False)
        if check.returncode:
            raise SystemExit("Existing venv has an incompatible Python/GPU runtime; use a new --venv directory.")
        common = [interpreter, "-m", "pip", "--isolated", "--disable-pip-version-check", "install"]
        subprocess.run([*common, "--only-binary=:all:", "--upgrade", "pip", "setuptools>=77", "wheel"], check=True)
        reinstall = ["--force-reinstall"] if repair else []
        subprocess.run([*common, "--only-binary=:all:", *reinstall, "-r", str(root / "requirements-portable.txt")], check=True)
        subprocess.run([*common, "--no-deps", "--no-build-isolation", "-e", str(root) + "[downloader]"], check=True)
        subprocess.run([interpreter, "-m", "pip", "check"], check=True)
        subprocess.run([interpreter, "-B", "-c", "import httpx,pydantic,dotenv,PIL.Image,numpy,cv2,onnxruntime,insightface,pycognito,yt_dlp,yt_dlp_ejs; onnxruntime.get_available_providers()"], check=True)
        for name, local in ((".env.portable.local", True), (".env.portable", False)):
            write_example(root / name, "worker-" + uuid.uuid4().hex, local=local)
    finally:
        lock.rmdir()
    print("Python environment installed. Models, capabilities and authentication are not yet verified; nothing was started.")
    print(system_help())
    print("Next: python scripts/setup-worker.py doctor --load-models")
    return 0


@contextmanager
def configuration(env_file: Path | None):
    """CLI-only explicit configuration scope; never search for a default .env."""
    previous = os.environ.copy()
    try:
        # Preserve OS/PATH settings, but never accidentally inherit another
        # Worker's identity, credentials, model paths or mock/update flags.
        prefixes = ("WORKER_", "NODE_", "FACESWAP_", "CUTOUT_", "ENHANCE_", "DOWNLOAD_", "ONNX_", "MEDIAWORKER_", "MAX_")
        names = {"API_BASE_URL", "GITHUB_TOKEN", "MODELS_DIR", "CAPABILITIES", "CONCURRENCY", "LOG_FILE", "WORK_DIR", "TRUST_TIER", "POLL_INTERVAL_MS", "CLAIM_TIMEOUT_S"}
        for key in list(os.environ):
            if key in names or key.startswith(prefixes):
                del os.environ[key]
        if env_file is not None:
            from dotenv import dotenv_values
            if not env_file.is_file():
                raise ValueError("configuration_file_missing")
            # No expansion from ambient secrets or other config files.
            values = dotenv_values(env_file, interpolate=False)
            os.environ.update({key: value for key, value in values.items() if value is not None})
        from .config import Config
        cfg = Config.from_env(load_env=False)
        cfg.auto_update = cfg.faceswap_model_auto_download = cfg.cutout_model_auto_download = False
        yield cfg
    finally:
        os.environ.clear()
        os.environ.update(previous)


def authentication(cfg, *, explicit: bool) -> dict:
    if not explicit:
        return {"status": "not_checked", "hint": "For private mode pass --env-file explicitly; local mode needs no account."}
    required = ("NODE_ID", "NODE_RUNTIME_DIR", "NODE_TOKEN_FILE")
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if not cfg.api_base_url:
        missing.append("API_BASE_URL")
    if not cfg.node_token:
        for key in ("WORKER_COGNITO_USERNAME", "WORKER_COGNITO_PASSWORD", "WORKER_COGNITO_USER_POOL_ID", "WORKER_COGNITO_CLIENT_ID"):
            if not os.environ.get(key, "").strip():
                missing.append(key)
    if missing:
        return {"status": "missing_configuration", "fields": missing}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", cfg.node_id):
        return {"status": "invalid_node_identity", "hint": "Use a unique 1..128 character node ID: letters/digits first, then letters/digits/._:-."}
    origin = urlsplit(cfg.api_base_url)
    if (not origin.hostname or origin.username or origin.password or origin.query or origin.fragment
            or origin.path not in {"", "/"} or origin.scheme not in {"https", "http"}
            or origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1", "::1"}):
        return {"status": "invalid_api_origin", "hint": "Set an HTTPS Worker API origin without credentials, path, query or fragment (HTTP only for loopback testing)."}
    if cfg.node_token_file.parent.resolve() != cfg.runtime_dir.resolve():
        return {"status": "state_path_conflict", "hint": "Place NODE_TOKEN_FILE inside this node's dedicated NODE_RUNTIME_DIR."}
    legacy = Path.home() / ".mediaworker"
    if cfg.runtime_dir.resolve() == legacy.resolve():
        return {"status": "shared_legacy_state", "hint": "Use a new unique NODE_ID and a dedicated state directory. Preserve old caches."}
    if cfg.node_token_file.is_file():
        try:
            cached = json.loads(cfg.node_token_file.read_text(encoding="utf-8"))
            if cached.get("node_id") != cfg.node_id or cached.get("api_base_url") != cfg.api_base_url:
                return {"status": "node_cache_conflict", "hint": "Do not reuse another node's cache or revoked identity. Choose new explicit node/state settings."}
        except (ValueError, OSError, AttributeError):
            return {"status": "node_cache_unreadable", "hint": "Preserve the cache; inspect node/state settings before starting."}
    return {"status": "configured_not_authenticated", "hint": "Offline check only. Account password, worker-group access, enrollment and allowed capabilities must be confirmed by your administrator."}


def auth_failure(code: str) -> dict:
    if code in {"NEW_PASSWORD_REQUIRED", "FORCE_CHANGE_PASSWORD", "UserNotConfirmedException"}:
        return {"status": "account_setup_required", "hint": "Complete the permanent-password/account confirmation flow; a temporary password cannot enroll this Worker."}
    if code == "403":
        return {"status": "access_not_authorized", "hint": "Administrator must check worker-group access, account/enrollment policy, allowed capabilities and revoked/owned node identity. Do not reuse another node."}
    if code == "404":
        return {"status": "worker_api_not_found", "hint": "Use the origin exposing /api/worker/login; the public website may be a different origin."}
    if code in {"401", "NotAuthorizedException"}:
        return {"status": "authentication_rejected", "hint": "Check credentials/account setup with the administrator. Do not paste credentials into logs."}
    return {"status": "authentication_not_verified", "hint": "No online check was performed. See the administrator's authentication error without sharing secrets."}


def model_inventory(cfg, capability: str, mode: str) -> dict:
    if capability == "download":
        return {"status": "not_required", "missing": []}
    from .engine.faceswap_engine import IDENTITY_MODEL, _swap_model_paths
    if capability == "faceswap":
        missing = []
        if not (cfg.faceswap_models_dir / IDENTITY_MODEL).is_file():
            missing.append(IDENTITY_MODEL)
        if not any(path.is_file() for path in _swap_model_paths(cfg).values()):
            missing.append("one licensed inswapper_128[_fp16].onnx or hyperswap_1[a-c]_256.onnx")
        if mode == "local" and not (cfg.enhance_models_dir / "yoloface_8n.onnx").is_file():
            missing.append("enhance/yoloface_8n.onnx")
    elif capability == "cutout":
        from .engine.cutout_engine import MODEL_FILES
        filename = MODEL_FILES.get(cfg.cutout_model)
        missing = [] if filename and (cfg.cutout_models_dir / filename).is_file() else [filename or "supported cutout model"]
    else:
        from .engine.enhance_engine import MODEL_SPECS, FACE_FILES
        files = [spec.filename for name in cfg.enhance_models if name in MODEL_SPECS for spec in MODEL_SPECS[name]]
        files += [FACE_FILES[name] for name in cfg.enhance_face_models if name in FACE_FILES]
        missing = [name for name in files if not (cfg.enhance_models_dir / name).is_file()]
    return {"status": "missing" if missing else "files_present_unverified", "missing": missing,
            "hint": "Place licensed models as described in docs/local-mode.md. No models were downloaded."}


def inspect(cfg, *, mode: str, explicit: bool, load_models: bool = False) -> dict:
    report = {"platform": {"os": sys.platform, "architecture": platform.machine(),
                           "python": platform.python_version(), "problem": platform_problem()},
              "installation": {}, "providers": [], "capabilities": {},
              "authentication": authentication(cfg, explicit=explicit) if mode == "distributed" else {"status": "not_needed"},
              "execution": "not_started_by_doctor"}
    for name in IMPORTS:
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                module = import_module(name)
            report["installation"][name] = "imported"
            if name == "onnxruntime":
                report["providers"] = module.get_available_providers()
        except Exception:
            report["installation"][name] = "import_failed"
    installed = all(value == "imported" for value in report["installation"].values())
    report["installation_ok"] = installed and report["platform"]["problem"] is None
    report["system_dependency_help"] = system_help()
    for cap in CAPABILITIES:
        if cap not in cfg.capabilities:
            report["capabilities"][cap] = {"status": "not_configured"}
            continue
        if not installed:
            report["capabilities"][cap] = {"status": "unavailable", "reason": "Fix Python imports; Linux OpenCV may need libgl1/libglib2.0-0."}
            continue
        if cap == "enhance" and not ({"DmlExecutionProvider", "CUDAExecutionProvider"} & set(report["providers"]) & set(cfg.onnx_providers)):
            report["capabilities"][cap] = {"status": "unsupported_provider", "reason": "Enhance requires DirectML/CUDA; CPU/CoreML does not satisfy this engine."}
            continue
        try:
            files = model_inventory(cfg, cap, mode)
            item = {"status": "unverified", "models": files}
            if cap == "download":
                from .downloader.runtime import check_runtime
                from .downloader.worker import Config as DownloadConfig
                check_runtime(DownloadConfig.from_env())
                item = {"status": "ready", "models": files, "hint": "Local tools/encoders checked; no download request was made."}
            elif files["missing"]:
                item["status"] = "missing_models"
            elif load_models:
                from .engine.registry import ENGINE_FACTORIES
                from .local_faceswap import LocalFaceSwap
                engine = LocalFaceSwap(cfg) if cap == "faceswap" and mode == "local" else ENGINE_FACTORIES[cap](cfg)
                # No model sync, credentials, HTTP client or task scheduler here.
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    engine.load()
                item["status"] = "ready"
                item["loaded_models"] = engine.available_models
                if cap == "faceswap" and mode == "distributed":
                    item["media"] = engine.available_media
                if cap == "enhance" and not engine.available_models:
                    item["status"] = "unavailable"
            report["capabilities"][cap] = item
        except Exception:
            report["capabilities"][cap] = {"status": "unavailable", "reason": "Local engine/tool validation failed; check model compatibility, configured paths and system dependencies. No task was submitted."}
    ready = [cap for cap, item in report["capabilities"].items() if item["status"] == "ready"]
    report["ready_capabilities"] = ready
    report["all_configured_capabilities_ready"] = bool(cfg.capabilities) and set(cfg.capabilities) <= set(ready)
    report["model_loading_requested"] = load_models
    report["overall"] = ("installation_failed" if not report["installation_ok"] else
                         "configured_capabilities_ready" if report["all_configured_capabilities_ready"] else
                         "installed_with_unverified_or_unavailable_capabilities")
    return report


def start(cfg, *, mode: str, explicit: bool, port: int, env_file: Path | None) -> int:
    from .single_instance import FileLock, WorkerAlreadyRunning
    try:
        with FileLock(cfg.runtime_dir / "portable-worker.lock"):
            return _start(cfg, mode=mode, explicit=explicit, port=port)
    except WorkerAlreadyRunning:
        print("Another portable Worker owns this node's state directory; do not start a duplicate.")
        return 1


def _start(cfg, *, mode: str, explicit: bool, port: int) -> int:
    if mode == "distributed":
        auth = authentication(cfg, explicit=explicit)
        if auth["status"] != "configured_not_authenticated":
            print(json.dumps({"authentication": auth}, indent=2))
            return 1
    report = inspect(cfg, mode=mode, explicit=explicit)
    if not report["installation_ok"]:
        print(json.dumps(report, indent=2))
        return 1
    eligible = [cap for cap, item in report["capabilities"].items() if item["status"] in {"unverified", "ready"}]
    if not eligible:
        print(json.dumps(report, indent=2))
        print("No capability is ready to load. Install licensed models or repair download dependencies first.")
        return 1
    os.environ["CAPABILITIES"] = ",".join(eligible)
    os.environ["WORKER_AUTO_UPDATE"] = "false"
    os.environ["FACESWAP_MODEL_AUTO_DOWNLOAD"] = os.environ["CUTOUT_MODEL_AUTO_DOWNLOAD"] = "false"
    if mode == "local":
        from .local_web import run
        # Configuration is already explicit; prevent a second dotenv load.
        run(argparse.Namespace(port=port, env_file=None))
    else:
        from .__main__ import _configure_logging
        from .runner import run_forever
        cfg.capabilities, cfg.claim_capabilities = eligible, None
        _configure_logging()
        asyncio.run(run_forever(cfg, available_only=True))
    return 0


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "doctor", "start"])
    parser.add_argument("--venv", type=Path, default=ROOT / ENV_NAME)
    parser.add_argument("--mode", choices=["local", "distributed"], default="local")
    parser.add_argument("--env-file", type=Path, help="Explicit configuration; never searches for .env.")
    parser.add_argument("--load-models", action="store_true", help="Doctor only: load installed licensed models locally, without downloads/authentication.")
    parser.add_argument("--require-ready", action="store_true", help="Doctor only: fail unless every configured capability passed local loading/tool checks.")
    parser.add_argument("--auth-error", help="Doctor only: classify a previously observed auth code offline, e.g. 403 or FORCE_CHANGE_PASSWORD.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repair", action="store_true", help="Install only: reinstall this profile's wheel dependencies to repair missing/corrupt installed files.")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    if args.command == "install":
        try:
            raise SystemExit(install(ROOT, args.venv.absolute(), repair=args.repair))
        except subprocess.CalledProcessError:
            raise SystemExit("Installation/import validation failed. Configuration is preserved. Fix the reported wheel/network/file-access issue, then rerun install; use install --repair for missing/corrupt package files or choose a new --venv directory. Do not weaken system permissions/protection.") from None
    interpreter = python_in(args.venv.resolve())
    if Path(sys.prefix).resolve() != args.venv.resolve():
        if not interpreter.is_file():
            raise SystemExit("Environment missing. Run: python scripts/setup-worker.py install")
        result = subprocess.run([str(interpreter), "-B", str(ROOT / "scripts/setup-worker.py"), *(sys.argv[1:] if argv is None else argv)], check=False)
        raise SystemExit(result.returncode)
    env_file = args.env_file
    if env_file is None:
        generated = ROOT / (".env.portable.local" if args.mode == "local" else ".env.portable")
        if generated.is_file():
            env_file = generated
    logging.disable(logging.CRITICAL) if args.command == "doctor" else None
    try:
        with configuration(env_file) as cfg:
            if args.command == "doctor":
                report = inspect(cfg, mode=args.mode, explicit=env_file is not None, load_models=args.load_models)
                if args.auth_error:
                    report["observed_auth_error"] = auth_failure(args.auth_error)
                print(json.dumps(report, indent=2))
                success = report["installation_ok"] and (not args.require_ready or report["all_configured_capabilities_ready"])
                raise SystemExit(0 if success else 1)
            raise SystemExit(start(cfg, mode=args.mode, explicit=env_file is not None, port=args.port, env_file=env_file))
    except (ValueError, OSError):
        raise SystemExit("Configuration is invalid or unavailable. Check your explicitly selected file; values are hidden.") from None


if __name__ == "__main__":
    main()
