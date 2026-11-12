#!/usr/bin/env bash
# Keep the published Termux install URL usable; both hosts use the same installer.
set -euo pipefail
if [[ -z "${PREFIX:-}" || ! -x "$PREFIX/bin/pkg" ]]; then
    printf 'Run this installer inside Termux on Android.\n' >&2
    exit 1
fi
curl -fsSL https://raw.githubusercontent.com/OpenHCSDev/agent-comms/main/tools/install-toad.sh | bash
