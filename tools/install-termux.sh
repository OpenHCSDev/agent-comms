#!/usr/bin/env bash
# Install the local Toad frontend; networking remains Toad/Tailscale's responsibility.
set -euo pipefail

if [[ -z "${PREFIX:-}" || ! -x "$PREFIX/bin/pkg" ]]; then
    printf 'Run this installer inside Termux on Android.\n' >&2
    exit 1
fi

installation="$HOME/.local/share/toad-comms"
source="$installation/source"
mkdir -p "$installation"
exec > >(tee -a "$installation/install.log") 2>&1
trap 'printf "\nInstallation stopped. Log: %s/install.log\n" "$installation" >&2' ERR

printf 'Installing Toad Comms and its build dependencies…\n'
pkg update -y
# psutil and Pillow need Termux's Android patches, not desktop Linux wheels.
pkg install -y python python-pip uv git openssh clang make cmake ninja rust \
    pkg-config libffi openssl libjpeg-turbo zlib python-psutil python-pillow

if [[ -d "$source/.git" ]]; then
    # uv owns the generated lock; never discard edits to the source or a conflicting lock.
    if ! git -C "$source" diff --quiet HEAD -- . ':!stack/uv.lock'; then
        printf 'Source has local edits: %s. Keeping them; installation stopped.\n' "$source" >&2
        exit 1
    fi
    git -C "$source" fetch --depth 1 origin main
    git -C "$source" merge --ff-only FETCH_HEAD
else
    git clone --depth 1 https://github.com/OpenHCSDev/agent-comms.git "$source"
fi

# The stack owns every Python package/source selection. Refresh its tracked branches,
# retaining its PyPI pins, exactly as the desktop installer does.
mapfile -t upgrades < <("$PREFIX/bin/python" - "$source/stack/pyproject.toml" <<'PY'
import sys
import tomllib
with open(sys.argv[1], "rb") as stream:
    sources = tomllib.load(stream)["tool"]["uv"]["sources"]
for name, source in sources.items():
    if "branch" in source:
        print("--upgrade-package")
        print(name)
PY
)

cd "$source/stack"
# Inherit the patched native packages without letting pip change Termux's system Python.
"$PREFIX/bin/python" -m venv --system-site-packages .venv
uv export --project . --python "$PREFIX/bin/python" --no-managed-python \
    --no-python-downloads --no-dev --no-hashes --no-emit-project \
    "${upgrades[@]}" --output-file "$installation/requirements.txt" >/dev/null
.venv/bin/python -m pip install --prefer-binary -r "$installation/requirements.txt"
.venv/bin/python -m pip check
.venv/bin/python -c 'import toad.cli; print("Toad imports successfully.")'

mkdir -p "$installation/bin" "$HOME/.shortcuts"
{
    printf '#!%s/bin/bash\n' "$PREFIX"
    cat "$source/stack/bin/toad-comms-termux" | tail -n +2
} > "$installation/bin/toad-comms"
chmod 755 "$installation/bin/toad-comms"
ln -sfn "$installation/bin/toad-comms" "$PREFIX/bin/toad-comms"
ln -sfn "$installation/bin/toad-comms" "$HOME/.shortcuts/Toad Comms"

printf '\nInstalled. Run: toad-comms\n'
printf 'Termux:Widget shortcut: Toad Comms\n'
printf 'To update, run the same installer again.\n'
