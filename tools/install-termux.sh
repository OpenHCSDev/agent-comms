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
# Termux supports complete package upgrades, not mixed library/consumer versions.
pkg upgrade -y
# Use Termux's patched native modules and numerical libraries.
pkg install -y python python-pip uv git openssh neovim clang make cmake ninja rust \
    pkg-config libffi openssl libjpeg-turbo zlib python-psutil python-pillow python-numpy

if [[ -d "$source/.git" ]]; then
    # uv owns the generated lock; source edits are never discarded.
    if ! git -C "$source" diff --quiet HEAD -- . ':!stack/uv.lock'; then
        printf 'Source has local edits: %s. Keeping them; installation stopped.\n' "$source" >&2
        exit 1
    fi
    if ! git -C "$source" diff --quiet HEAD -- stack/uv.lock; then
        # Keep the previous native resolution before restoring the tracked input for Git.
        # Otherwise a later main changing uv.lock cannot fast-forward this checkout.
        previous_lock="$(mktemp "$installation/uv.lock.XXXXXXXX")"
        cp "$source/stack/uv.lock" "$previous_lock"
        git -C "$source" restore --source=HEAD -- stack/uv.lock
        printf 'Previous native lock retained: %s\n' "$previous_lock"
    fi
    git -C "$source" fetch origin main
    git -C "$source" merge --ff-only FETCH_HEAD
else
    git clone --depth 1 https://github.com/OpenHCSDev/agent-comms.git "$source"
fi

cd "$source/stack"
# Android can refuse a just-linked build script during parallel Cargo builds (ETXTBSY).
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"
# Resolution and pip use separate source trees; share compiled Rust dependencies.
export CARGO_TARGET_DIR="$installation/cargo-target"
export PIP_CACHE_DIR="$installation/pip-cache"
mkdir -p "$installation/wheels"
# Retained native builds use normal wheel tags; incompatible ABIs are not selected.
export PIP_FIND_LINKS="$installation/wheels https://github.com/OpenHCSDev/agent-comms/releases/expanded_assets/termux-wheels"
# The Termux community index supplies Android wheels absent from desktop PyPI.
termux_wheels=https://termux-user-repository.github.io/pypi/
export PIP_EXTRA_INDEX_URL="$termux_wheels"
export PIP_PREFER_BINARY=1
export PIP_ONLY_BINARY=maturin
# Inherit the patched native packages without letting pip change Termux's system Python.
"$PREFIX/bin/python" -m venv --system-site-packages .venv
# Resolve this platform from the declared constraints, including transitive branches;
# an old desktop lock may pin releases from before Android wheels were available.
uv export --project . --python "$PREFIX/bin/python" --no-managed-python \
    --no-python-downloads --no-dev --no-hashes --no-emit-project \
    --index "$termux_wheels" --index-strategy unsafe-best-match \
    --no-build-package maturin \
    --upgrade --output-file "$installation/requirements.txt" >/dev/null
# uv resolved the complete graph, including source overrides. pip installs that answer;
# resolving again treats a branch URL and its selected commit as competing dependencies.
# A resolved source revision must replace an installed wheel even when its version
# is unchanged. Keep Termux's native index packages out of that forced reinstall.
index_requirements="$installation/index-requirements.txt"
source_requirements="$installation/source-requirements.txt"
awk -v sources="$source_requirements" '
    BEGIN { print "" > sources }
    $2 == "@" { print > sources; next }
    { print }
' "$installation/requirements.txt" > "$index_requirements"
.venv/bin/python -m pip install --upgrade --no-deps --prefer-binary -r "$index_requirements"
.venv/bin/python -m pip install --force-reinstall --no-deps --prefer-binary -r "$source_requirements"
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
