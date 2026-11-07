"""Run with the protected installation tools Python; no account input needed."""
import json
import os
from pathlib import Path
import re
import sys

if sys.platform == "win32":
    root = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "ClapClipWorker"
elif sys.platform == "darwin":
    root = Path("/Library/Application Support/ClapClipWorker")
else:
    root = Path("/var/lib/clapclip-worker")
host = json.loads((root / "host.json").read_text(encoding="utf-8"))
data = Path("/var/lib/clapclip-worker-runtime") if host["service_user"] == "dynamic" else root / "data"
revision = json.loads((data / "selected.json").read_text(encoding="utf-8"))["revision"]
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit("INVALID_OFFICIAL_REVISION")
release = data / "releases" / revision
python = release / ".venv-managed" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
os.chdir(release)
os.execv(str(python), [str(python), "-B", "-m", "mediaworker.managed", *(sys.argv[1:] or ["status"])])
