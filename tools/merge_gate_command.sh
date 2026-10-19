#!/usr/bin/env bash
# Only this fixed command belongs in the loopback-only authorized_keys entry.
# No forwarded environment, arbitrary shell command, or workflow stdin is used.
set -euo pipefail
state=${1:?usage: merge_gate_command.sh STATE_ROOT MAIN_CHECKOUT PYTHON}
repo=${2:?main checkout required}
python=${3:?shared check interpreter required}
mkdir -p "$state/host-logs"
log=$(mktemp "$state/host-logs/check-XXXXXXXX.log")
exec </dev/null
# The host owns these logs; the runner namespace cannot see them.
if env -i HOME=/home/ts USER=ts PATH=/usr/bin:/bin LANG=C.UTF-8 \
  PYTHONDONTWRITEBYTECODE=1 TMPDIR="$state" \
  PYTHONPATH="$repo/src:$repo/tools:/home/ts/wt/pi-install-gate--P2/scripts" \
  timeout 300 "$python" -X faulthandler "$(dirname "$0")/merge_queue.py" \
  --repo "$repo" --scratch "$state" -- "${SSH_ORIGINAL_COMMAND:?missing request}" \
  >"$log" 2>&1; then
  printf 'check passed\n'
else
  printf 'check failed; private diagnostics retained on ts-gaming\n'
  exit 1
fi
