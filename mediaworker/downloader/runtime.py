"""Fail fast without touching network or GPU models."""
from importlib import metadata
import re
import shutil
import subprocess
import sys


def check_runtime(config):
    if sys.version_info < (3, 13):
        raise RuntimeError("Download mode requires Python 3.13+ for current public-IP classifications; keep AI mode on its existing Python.")
    try:
        version = metadata.version("yt-dlp")
        metadata.version("yt-dlp-ejs")
    except metadata.PackageNotFoundError:
        raise RuntimeError("Install the mediaworker[downloader] dependencies.") from None
    versions = {}
    for name, binary, flag in (("node", config.node, "--version"), ("ffmpeg", config.ffmpeg, "-version"), ("ffprobe", config.ffprobe, "-version")):
        resolved = shutil.which(binary) if binary else None
        if not resolved:
            raise RuntimeError(f"Install {name}, or set DOWNLOAD_{name.upper()} to its executable.")
        try:
            result = subprocess.run([resolved, flag], capture_output=True, text=True, timeout=10,
                                    check=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError(f"{name} could not start; check the configured executable.") from None
        line = result.stdout.splitlines()[0] if result.stdout else ""
        if name == "node" and (not re.match(r"v\d+\.", line) or int(line.split(".")[0][1:]) < 22):
            raise RuntimeError("Download mode requires Node.js 22+ for the official YouTube JavaScript solver.")
        setattr(config, name, resolved)
        versions[name] = line[:200]
    try:
        encoders = subprocess.run([config.ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
            text=True, check=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0).stdout
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Download could not inspect local ffmpeg encoders.") from None
    required = {"libx264", "libvpx-vp9", "aac", "libopus", "libmp3lame"}
    available = {line.split()[1] for line in encoders.splitlines() if len(line.split()) >= 2}
    if not required <= available:
        raise RuntimeError("Download requires ffmpeg encoders: " + ", ".join(sorted(required - available)))
    return {"status": "ok", "python": sys.version.split()[0], "yt_dlp": version, **versions}
