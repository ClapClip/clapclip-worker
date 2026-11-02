#!/usr/bin/env bash
# Idempotent Ubuntu 22.04/24.04 install for a CUDA mediaworker node.
# Does not contain secrets and does not start or enable the service.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/linux/install.sh" >&2
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
if [[ "${ID:-}" != "ubuntu" || ( "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" ) ]]; then
  echo "This script supports Ubuntu 22.04 and 24.04 (found ${ID:-unknown} ${VERSION_ID:-})." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX="${PREFIX:-$ROOT}"
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install --yes ca-certificates curl ffmpeg git build-essential

if ! command -v python3.13 >/dev/null 2>&1; then
  apt-get install --yes software-properties-common
  if ! add-apt-repository --yes ppa:deadsnakes/ppa \
      || ! apt-get update \
      || ! apt-get install --yes python3.13 python3.13-venv python3.13-dev; then
    echo "deadsnakes did not provide Python 3.13; installing uv" >&2
    export PATH="/usr/local/bin:/root/.local/bin:${PATH}"
    if ! command -v uv >/dev/null 2>&1; then
      curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
    fi
    uv python install 3.13
  fi
fi
hash -r
export PATH="/usr/local/bin:/root/.local/bin:${PATH}"

if command -v python3.13 >/dev/null 2>&1; then
  PY313="$(command -v python3.13)"
else
  PY313="$(uv python find 3.13)"
fi

if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install --yes nodejs
fi

if ! id mediaworker >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/mediaworker --shell /usr/sbin/nologin mediaworker
fi
install -d -o mediaworker -g mediaworker -m 750 \
  /var/lib/mediaworker /var/lib/mediaworker/logs /var/lib/mediaworker/.mediaworker

"$PY313" -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/python" -m pip install --upgrade pip
"$PREFIX/.venv/bin/python" -m pip install -r "$ROOT/requirements-unified-linux-cuda.txt"
"$PREFIX/.venv/bin/python" -m pip install --no-deps -e "$ROOT" "insightface==2.0"

if [[ ! -e "$PREFIX/.env" ]]; then
  install -m 640 -o mediaworker -g mediaworker "$ROOT/deploy/linux/env.example" "$PREFIX/.env"
  echo "Wrote $PREFIX/.env from deploy/linux/env.example. Replace placeholders before starting."
else
  echo "Keeping existing $PREFIX/.env"
fi

python3 - "$ROOT/deploy/linux/mediaworker.service" /etc/systemd/system/mediaworker.service "$PREFIX" <<'PY'
import pathlib, sys
source, destination, prefix = sys.argv[1:]
text = pathlib.Path(source).read_text(encoding="utf-8").replace("@PREFIX@", prefix)
pathlib.Path(destination).write_text(text, encoding="utf-8")
PY
chmod 644 /etc/systemd/system/mediaworker.service
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
fi

echo "Installed the unit but did not start it."
echo "Edit $PREFIX/.env, then: systemctl enable --now mediaworker.service"
