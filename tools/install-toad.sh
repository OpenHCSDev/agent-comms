#!/usr/bin/env bash
# Install the local frontend in Termux or Android Terminal's Debian VM.
set -Eeuo pipefail

installation="$HOME/.local/share/toad-comms"
source="$installation/source"
mkdir -p "$installation"
exec > >(tee -a "$installation/install.log") 2>&1
trap 'printf "\nInstallation stopped. Log: %s/install.log\n" "$installation" >&2' ERR

# Decode the host once. The rest of the installation calls the selected operations.
if [[ -n "${PREFIX:-}" && -x "$PREFIX/bin/pkg" ]]; then
    uv="$PREFIX/bin/uv"
    install_dependencies() {
        pkg upgrade -y
        pkg install -y python python-pip uv git openssh neovim clang make cmake ninja rust \
            pkg-config libffi openssl libjpeg-turbo zlib python-psutil python-pillow python-numpy
    }
    sync_environment() {
        # Android can refuse a just-linked Cargo build script during parallel builds.
        export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"
        export CARGO_TARGET_DIR="$installation/cargo-target"
        export PIP_CACHE_DIR="$installation/pip-cache"
        mkdir -p "$installation/wheels"
        export PIP_FIND_LINKS="$installation/wheels https://github.com/OpenHCSDev/agent-comms/releases/expanded_assets/termux-wheels"
        export PIP_EXTRA_INDEX_URL=https://termux-user-repository.github.io/pypi/
        export PIP_PREFER_BINARY=1
        export PIP_ONLY_BINARY=maturin
        "$PREFIX/bin/python" -m venv --system-site-packages .venv
        "$uv" export --project . --python "$PREFIX/bin/python" --no-managed-python \
            --no-python-downloads --no-dev --no-hashes --no-emit-project \
            --index "$PIP_EXTRA_INDEX_URL" --index-strategy unsafe-best-match \
            --no-build-package maturin \
            --upgrade --output-file "$installation/requirements.txt" >/dev/null
        # Install uv's answer without resolving the direct sources a second time.
        # Same-version source builds must be replaced; native index wheels need not be.
        local index_requirements="$installation/index-requirements.txt"
        local source_requirements="$installation/source-requirements.txt"
        awk -v sources="$source_requirements" '
            BEGIN { print "" > sources }
            $2 == "@" { print > sources; next }
            { print }
        ' "$installation/requirements.txt" > "$index_requirements"
        .venv/bin/python -m pip install --upgrade --no-deps --prefer-binary -r "$index_requirements"
        .venv/bin/python -m pip install --force-reinstall --no-deps --prefer-binary -r "$source_requirements"
        .venv/bin/python -m pip check
    }
    link_launchers() {
        ln -sfn "$installation/bin/toad-comms" "$PREFIX/bin/toad-comms"
        mkdir -p "$HOME/.shortcuts"
        ln -sfn "$installation/bin/toad-comms" "$HOME/.shortcuts/Toad Comms"
    }
elif command -v apt-get >/dev/null; then
    uv="$HOME/.local/bin/uv"
    install_dependencies() {
        sudo apt-get update
        sudo apt-get install -y --no-install-recommends \
            curl git build-essential pkg-config libffi-dev libssl-dev
        curl -fsSL https://astral.sh/uv/install.sh -o "$installation/uv-installer.sh"
        sh "$installation/uv-installer.sh"
    }
    sync_environment() {
        # The phone VM may have only 1 GB RAM. Linux wheels avoid Android-only builds.
        UV_CONCURRENT_BUILDS=1 UV_CONCURRENT_DOWNLOADS=4 \
            "$uv" sync --project . --upgrade
        "$uv" pip check --python .venv/bin/python
    }
    link_launchers() {
        # Existing Terminal shells do not yet have uv's new PATH entry.
        sudo mkdir -p /usr/local/bin
        sudo ln -sfn "$installation/bin/toad-comms" /usr/local/bin/toad-comms
        sudo ln -sfn "$source/stack/.venv/bin/toad" /usr/local/bin/toad
    }
else
    printf 'Run this installer in Termux or Android Terminal’s Debian VM.\n' >&2
    exit 1
fi

printf 'Installing Toad Comms and its build dependencies…\n'
install_dependencies

if [[ -d "$source/.git" ]]; then
    if ! git -C "$source" diff --quiet HEAD -- . ':!stack/uv.lock'; then
        printf 'Source has local edits: %s. Keeping them; installation stopped.\n' "$source" >&2
        exit 1
    fi
    if ! git -C "$source" diff --quiet HEAD -- stack/uv.lock; then
        previous_lock="$(mktemp "$installation/uv.lock.XXXXXXXX")"
        cp "$source/stack/uv.lock" "$previous_lock"
        git -C "$source" restore --source=HEAD -- stack/uv.lock
        printf 'Previous resolution retained: %s\n' "$previous_lock"
    fi
    git -C "$source" fetch origin main
    git -C "$source" merge --ff-only FETCH_HEAD
else
    git clone --depth 1 https://github.com/OpenHCSDev/agent-comms.git "$source"
fi

cd "$source/stack"
sync_environment
.venv/bin/python -c 'import toad.cli; print("Toad imports successfully.")'

mkdir -p "$installation/bin"
{
    printf '#!%s\n' "$(command -v bash)"
    tail -n +2 "$source/stack/bin/toad-local"
} > "$installation/bin/toad-comms"
chmod 755 "$installation/bin/toad-comms"
link_launchers

printf '\nInstalled. Run: toad-comms\n'
printf 'To update, run the same installer again.\n'
