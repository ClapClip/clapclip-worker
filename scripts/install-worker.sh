#!/bin/sh
# No arguments. Run from a normal terminal; sudo is the OS boot-service prompt.
set -eu
if [ "$#" -ne 0 ]; then echo 'Run this installer without arguments.' >&2; exit 1; fi
case "$(uname -s):$(uname -m)" in
  Darwin:arm64)
    if [ "$(sysctl -in sysctl.proc_translated 2>/dev/null || true)" = 1 ]; then echo 'Use a native Apple Silicon terminal.' >&2; exit 1; fi
    root='/Library/Application Support/ClapClipWorker'; target=osx-arm64
    digest=c547639f5caf742236641c362746abb4a3505774753ecd08fe341643670c4c01 ;;
  Linux:x86_64) root=/var/lib/clapclip-worker; target=linux-64; digest=701f39c06f8e6a670a0e186c8479b9e5c0502a2f0cda6cef65d83bdd2875e8bd ;;
  Linux:aarch64|Linux:arm64) root=/var/lib/clapclip-worker; target=linux-aarch64; digest=3bb56786e21b9b24024f599fe9185b4a59dc3f9f59937a42311ee583adbbb23c ;;
  *) echo 'Supported: native M-series macOS, or glibc Linux x64/ARM64 with systemd.' >&2; exit 1 ;;
esac
if [ "$(id -u)" -ne 0 ]; then
  exec sudo /bin/sh "$0"
fi
if [ "$target" != osx-arm64 ]; then
  command -v systemctl >/dev/null || { echo 'A systemd system host is required.' >&2; exit 1; }
  ldd --version 2>&1 | head -n 1 | grep -qi 'musl' && { echo 'musl is not supported.' >&2; exit 1; }
fi
# Never execute an administrator bootstrap from a pre-created unowned tree.
if [ -L "$root" ]; then echo 'Unsafe host directory.' >&2; exit 1; fi
if [ -e "$root" ]; then
  if [ ! -f "$root/bootstrap-owner.txt" ] || [ -L "$root/bootstrap-owner.txt" ] || [ "$(cat "$root/bootstrap-owner.txt")" != clapclip-bootstrap-v1 ]; then
    echo 'Existing host directory needs ownership review.' >&2; exit 1
  fi
  case "$target" in
    osx-arm64) owner=$(stat -f %u "$root"); mode=$(stat -f %Lp "$root") ;;
    *) owner=$(stat -c %u "$root"); mode=$(stat -c %a "$root") ;;
  esac
  [ "$owner:$mode" = 0:755 ] || { echo 'Existing host permissions need review.' >&2; exit 1; }
else
  umask 022
  mkdir "$root"
  chmod 755 "$root"
  printf '%s\n' clapclip-bootstrap-v1 > "$root/bootstrap-owner.txt"
fi
# Serialize native bootstrap too, before Python/its OS-lock implementation exists.
lock="$root/.bootstrap-lock"
mkdir "$lock" 2>/dev/null || { echo 'Installer already running or interrupted; review its bootstrap lock.' >&2; exit 1; }
temp=''
trap 'if [ -n "$temp" ]; then rm -f "$temp"; fi; rmdir "$lock"' EXIT HUP INT TERM
# Refuse known legacy workers before installing tools. Unknown Python argv are
# not guessed; the Python inventory repeats this check before enrollment.
if ps -axo command= | grep -E '[m]ediaworker (run|local|download-worker)|[m]ediaworker.supervisor|[C]lapClipWoker' >/dev/null; then
  echo 'Existing Worker requires reuse or controlled migration; nothing was started.' >&2; exit 1
fi
if [ ! -x "$root/micromamba" ]; then
  if ! command -v curl >/dev/null; then
    if command -v apt-get >/dev/null; then apt-get update; apt-get install -y curl ca-certificates
    elif command -v dnf >/dev/null; then dnf install -y curl ca-certificates
    elif command -v yum >/dev/null; then yum install -y curl ca-certificates
    elif command -v zypper >/dev/null; then zypper --non-interactive install curl ca-certificates
    else echo 'No supported native package manager can supply HTTPS bootstrap tools.' >&2; exit 1
    fi
  fi
  temp=$(mktemp "$root/micromamba.XXXXXX")
  curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error \
    "https://github.com/mamba-org/micromamba-releases/releases/download/2.10.0-0/micromamba-$target" -o "$temp"
  if command -v sha256sum >/dev/null; then actual=$(sha256sum "$temp" | cut -d ' ' -f 1); else actual=$(shasum -a 256 "$temp" | cut -d ' ' -f 1); fi
  [ "$actual" = "$digest" ] || { echo 'Tool bootstrap integrity check failed.' >&2; exit 1; }
  chmod 755 "$temp"
  mv "$temp" "$root/micromamba"
  temp=''
fi
if command -v sha256sum >/dev/null; then actual=$(sha256sum "$root/micromamba" | cut -d ' ' -f 1); else actual=$(shasum -a 256 "$root/micromamba" | cut -d ' ' -f 1); fi
[ "$actual" = "$digest" ] || { echo 'Existing bootstrap integrity check failed.' >&2; exit 1; }
export MAMBA_ROOT_PREFIX="$root/package-cache"
if [ ! -f "$root/tools/conda-meta/history" ]; then
  if [ "$target" = osx-arm64 ]; then extras=''; else extras='libgl glib'; fi
  "$root/micromamba" --no-rc create --yes --prefix "$root/tools" --override-channels -c conda-forge \
    'python=3.13' 'nodejs=22' 'ffmpeg=*=gpl_*' git pip $extras
fi
export PATH="$root/tools/bin:$PATH"
export PYTHONNOUSERSITE=1 MEDIAWORKER_LOAD_DOTENV=0 PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH PYTHONHOME GIT_CONFIG GIT_CONFIG_COUNT GIT_DIR GIT_WORK_TREE
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
if [ ! -d "$root/installer-source" ]; then
  "$root/tools/bin/git" -c core.hooksPath=/dev/null -c http.followRedirects=false \
    clone --single-branch --branch main --no-tags https://github.com/ClapClip/clapclip-worker.git "$root/installer-source"
fi
# This protected bootstrap checkout is never an executing release or a user repo.
origin=$("$root/tools/bin/git" -C "$root/installer-source" config --get remote.origin.url)
[ "$origin" = https://github.com/ClapClip/clapclip-worker.git ] || { echo 'Unexpected installer origin.' >&2; exit 1; }
[ -z "$("$root/tools/bin/git" -C "$root/installer-source" status --porcelain --untracked-files=all)" ] || { echo 'Installer source modified.' >&2; exit 1; }
"$root/tools/bin/python" -m venv "$root/installer-env"
"$root/installer-env/bin/python" -m pip --isolated --disable-pip-version-check install --only-binary=:all: -r "$root/installer-source/requirements-managed.txt"
cd "$root/installer-source"
"$root/installer-env/bin/python" -B scripts/install-worker.py
