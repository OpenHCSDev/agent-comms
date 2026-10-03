#!/usr/bin/env bash
# Build the pinned input-ID Pi copy on persistent disk. Never alter stock Pi.
set -euo pipefail
umask 077
# Preparation runs Node only for version/syntax checks, never ambient preloads.
unset NODE_OPTIONS NODE_PATH NODE_COMPILE_CACHE
export NODE_DISABLE_COMPILE_CACHE=1

stack_root=/home/ts/wt/comms-task-aware-native-bundle-20261002/stack
repo_root=$(cd -- "$stack_root/.." && pwd)
stock=${PI_STOCK_DIR:-$HOME/.local/pi-npm/lib/node_modules/@earendil-works/pi-coding-agent}
manifest="$stack_root/pi-native.sha256"
build_id=$(sha256sum "$manifest" | cut -c1-16)
target="$stack_root/.pi-native-$build_id"
bedrock=node_modules/@earendil-works/pi-ai/dist/api/bedrock-converse-stream.js

verify() (
    cd -- "$1/node_modules/@earendil-works/pi-coding-agent"
    sha256sum --check --status "$manifest" &&
        python3 "$repo_root/src/agent_comms/native_package.py" "$PWD"
)

# The experiment validates stock bytes, applies the pinned native ID patch with
# zero fuzz, and checks JS syntax. Its Bedrock transport edit is intentionally
# restored below: the running stack must not fork Pi's network layer.
prepared=$(
    PI_STOCK_DIR="$stock" "$repo_root/experiments/pi-context-proof/prepare-copied-pi.sh" \
        | sed -n 's/^PI_PACKAGE_DIR=//p'
)
scratch_root=${prepared%/node_modules/@earendil-works/pi-coding-agent}
if [[ "$prepared" != "${TMPDIR:-/var/tmp}"/agent-comms-pi-native-*/node_modules/@earendil-works/pi-coding-agent \
      || ! -d "$prepared" ]]; then
    printf 'Native Pi preparation did not return a validated package.\n' >&2
    exit 1
fi

stage=$(mktemp -d "$stack_root/.pi-native.stage.XXXXXXXX")
trap 'rm -rf -- "$stage" "$scratch_root"' EXIT
mv -- "$scratch_root/node_modules" "$stage/node_modules"
cp -- "$stock/$bedrock" "$stage/node_modules/@earendil-works/pi-coding-agent/$bedrock"
compaction=dist/core/compaction/compaction.js
cp -- "$stack_root/native-compaction-policy.mjs" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/agent-comms-policy.js"
session=dist/core/agent-session.js
python3 "$stack_root/patch-native-auto-compaction.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/$session"
python3 "$stack_root/patch-native-steering.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent"
manager=dist/core/session-manager.js
services=dist/core/agent-session-services.js
python3 "$stack_root/patch-native-model-config.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/$services"
python3 "$stack_root/patch-native-adaptive-settings.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent"
completion_api=node_modules/@earendil-works/pi-ai/dist/api
cp -- "$stack_root/native-context-budget.mjs" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/$completion_api/agent-comms-context-budget.js"
cp -- "$stack_root/native-request-input.mjs" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/$completion_api/agent-comms-request-input.js"
python3 "$stack_root/patch-native-context-budget.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/$completion_api/openai-completions.js"
patch --batch --fuzz=0 --no-backup-if-mismatch -p1 \
    -d "$stage/node_modules/@earendil-works/pi-coding-agent" < "$stack_root/native-generation-policy.patch"
rpc=dist/modes/rpc/rpc-mode.js
python3 "$stack_root/patch-native-selected-compaction-summary.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/$rpc"
# One SessionManager storage owner; replace eager loaders and their consumers together.
native_package="$stage/node_modules/@earendil-works/pi-coding-agent"
patch --batch --fuzz=0 --no-backup-if-mismatch -p1 -d "$native_package" < "$stack_root/native-session-storage.patch"
cp -- "$stack_root/native-session-entry-store.mjs" "$native_package/dist/core/session-entry-store.js"
cp -- "$stack_root/native-session-entry-store.d.ts" "$native_package/dist/core/session-entry-store.d.ts"
cp -- "$stack_root/native-proof-journal.mjs" "$native_package/dist/core/native-proof-journal.js"
cp -- "$stack_root/native-proof-schema.mjs" "$native_package/dist/core/native-proof-schema.js"
cp -- "$stack_root/native-session-context.mjs" "$native_package/dist/core/session-context.js"
cp -- "$stack_root/native-compaction-source.mjs" "$native_package/dist/core/compaction/agent-comms-source.js"
cp -- "$stack_root/native-compaction-source.d.ts" "$native_package/dist/core/compaction/agent-comms-source.d.ts"
cp -- "$repo_root/src/agent_comms/pi_project_bootstrap.mjs" \
    "$stage/node_modules/@earendil-works/pi-coding-agent/dist/agent-comms-project-bootstrap.mjs"
python3 "$stack_root/patch-native-turn-context.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent"
python3 "$stack_root/patch-native-file-artifact.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent"
patch --batch --fuzz=0 --no-backup-if-mismatch -p1 \
    -d "$stage/node_modules/@earendil-works/pi-coding-agent" < "$stack_root/native-request-progress-adapters.patch"
patch --batch --fuzz=0 --no-backup-if-mismatch -p1 \
    -d "$stage/node_modules/@earendil-works/pi-coding-agent" < "$stack_root/native-summary-prefix.patch"
python3 "$stack_root/patch-native-request-observation.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent"
python3 "$stack_root/prepare-native-import-boundary.py" \
    "$stage/node_modules/@earendil-works/pi-coding-agent"
node --check "$stage/node_modules/@earendil-works/pi-coding-agent/$manager"
node --check "$stage/node_modules/@earendil-works/pi-coding-agent/$bedrock"
node --check "$stage/node_modules/@earendil-works/pi-coding-agent/$compaction"
node --check "$stage/node_modules/@earendil-works/pi-coding-agent/$session"
node --check "$stage/node_modules/@earendil-works/pi-coding-agent/$rpc"
node --check "$stage/node_modules/@earendil-works/pi-coding-agent/$services"
capture_root=/home/ts/wt/comms-task-aware-native-bundle-20261002/.artifacts/original-request-construction573-native-20261003/fresh-unverified
[[ ! -e "$capture_root" ]]
mv -- "$stage" "$capture_root"
trap - EXIT
rmdir -- "$scratch_root"
printf 'FRESH_UNVERIFIED_ROOT=%s\n' "$capture_root"
