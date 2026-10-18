#!/usr/bin/env bash
# Review-only cutover command list for Core #796 + #795. Do not run piecemeal.
set -euo pipefail
umask 077

CORE_HEAD=fed95482d777d3d318f3ea53373b4221832d1714
EXPECTED_PIN=4d41264a06dbe33652e78551a592e1387186885c478fe5be07cc09610c002616
ROOT=/var/tmp/agent-comms-live-20260927-wzjtqhza
ROOT_ID=e206f3766e60451a989ca34df0e2a94b
OLD_PACKAGE=/home/ts/.local/share/agent-comms/native-current-8d58a2d9141404e9/node_modules/@earendil-works/pi-coding-agent
NEW_PACKAGE=/home/ts/.local/share/agent-comms/native-current-4d41264a06dbe336/node_modules/@earendil-works/pi-coding-agent
ROUTE_DIR=/home/ts/.local/state/agent-comms
ROUTE_PATH="$ROUTE_DIR/active-route.json"
START_ENV=/home/ts/.cache/agent-scratch/live-build/start-env
BACKUP_ROOT=/home/ts/.local/share/agent-comms/backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$BACKUP_ROOT/native-8d58a2-cutover-$STAMP"
CORE_REPO=/home/ts/wt/core-claude
RUNTIME=$(dirname "$(dirname "$(readlink -f /home/ts/.local/bin/agent-comms)")")
PYTHON=/home/ts/wt/core-route-counts/.venv/bin/python; export PYTHONPATH=/home/ts/wt/core-claude/src

# Preflight the exact reviewed source commitment and published immutable package.
test "$(git -C "$CORE_REPO" rev-parse "$CORE_HEAD^{commit}")" = "$CORE_HEAD"
test "$(git -C "$CORE_REPO" show "$CORE_HEAD:stack/pi-native.sha256" | sha256sum | cut -d' ' -f1)" = "$EXPECTED_PIN"
test -f "$NEW_PACKAGE/dist/cli.js"
manifest="$BACKUP_ROOT/.native-8d58a2-manifest-$STAMP"
git -C "$CORE_REPO" show "$CORE_HEAD:stack/pi-native.sha256" > "$manifest"
trap 'rm -f -- "$manifest"' EXIT
(
  cd "$NEW_PACKAGE"
  grep -v '^#' "$manifest" | sha256sum --check --strict --status
)

# The route directory inode is the publication lock. This blocks until every
# Toad, watcher and owner borrower has retired; the FD stays open through backup,
# route replacement and start-env replacement.
exec {route_fd}<"$ROUTE_DIR"
flock --exclusive "$route_fd"

# Preserve exact pre-cutover inputs before either publication changes.
install -d -m 0700 "$BACKUP_ROOT" "$BACKUP"
cp --no-dereference --preserve=mode,timestamps "$ROUTE_PATH" "$BACKUP/active-route.json"
cp --no-dereference --preserve=mode,timestamps "$START_ENV" "$BACKUP/start-env"
printf '%s\n' "$CORE_HEAD" > "$BACKUP/core-head"
printf '%s\n' "$EXPECTED_PIN" > "$BACKUP/manifest-sha256"
sync -f "$BACKUP"

# Replace the existing route only if both original route and start environment
# are still the expected 615 deployment. The Core owner performs atomic writes,
# fsyncs and a checked rollback if the second publication fails.
ROUTE_FD="$route_fd" ROOT="$ROOT" ROOT_ID="$ROOT_ID" \
OLD_PACKAGE="$OLD_PACKAGE" NEW_PACKAGE="$NEW_PACKAGE" \
ROUTE_PATH="$ROUTE_PATH" START_ENV="$START_ENV" \
"$PYTHON" - <<'PY'
import os
from dataclasses import replace
from pathlib import Path

from agent_comms.active_route import (
    ActiveRoute,
    _publish_active_route_locked,
    read_active_route,
)
from agent_comms.store_files import _atomic_write_text

route_fd = int(os.environ["ROUTE_FD"])
route_path = Path(os.environ["ROUTE_PATH"])
start_env = Path(os.environ["START_ENV"])
original = ActiveRoute(
    Path(os.environ["ROOT"]),
    os.environ["ROOT_ID"],
    Path(os.environ["OLD_PACKAGE"]),
)
target = replace(original, native_package=Path(os.environ["NEW_PACKAGE"]))
expected_env = (
    f"AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID={original.wire_root_id} "
    f"AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE={original.native_package}\n"
)
target_env = (
    f"AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID={target.wire_root_id} "
    f"AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE={target.native_package}\n"
)
if read_active_route(route_path) != original:
    raise SystemExit("active route is not the expected 615 deployment")
if start_env.read_text() != expected_env:
    raise SystemExit("start-env is not the expected 615 deployment")
_publish_active_route_locked(target, route_path, route_fd, expected=original)
try:
    _atomic_write_text(start_env, target_env, fsync_parent=True, mode=0o600)
except BaseException:
    if read_active_route(route_path) == target:
        _publish_active_route_locked(original, route_path, route_fd, expected=target)
    raise
if read_active_route(route_path) != target or start_env.read_text() != target_env:
    raise SystemExit("cutover verification failed")
print(target.native_package)
PY

# Keep the exclusive lock until both durable read-backs above completed.
flock --unlock "$route_fd"
exec {route_fd}<&-
rm -f -- "$manifest"
trap - EXIT
printf 'route and start-env published; backup=%s\n' "$BACKUP"
