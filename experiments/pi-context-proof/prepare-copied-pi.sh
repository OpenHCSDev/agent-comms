#!/usr/bin/env bash
# Reproducible, opt-in Pi 0.85.1 native-input fork. NEVER patches the installed Pi.
set -euo pipefail
STOCK=${PI_STOCK_DIR:-/home/ts/.local/pi-npm/lib/node_modules/@earendil-works/pi-coding-agent}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VERSION=$(node -e 'console.log(require(process.argv[1]).version)' "$STOCK/package.json")
if [[ "$VERSION" != 0.85.1 ]]; then echo "Expected Pi 0.85.1, got $VERSION" >&2; exit 1; fi
(
  cd "$STOCK"
  sha256sum -c --status <<'SHAS'
fb8a3981c20c8c0bbd42231b1c99a10335fb3858b659056b341954de9cfa467f  dist/core/agent-session.js
ccace64949db25379a43971ecea750c1b7ec6344e1bc31b9d5fe596ac2f1c9f3  dist/core/session-manager.js
e7e4724aa55c5aac73cf36793653b26736200e5c59d58373990fc31028f86477  dist/modes/rpc/rpc-mode.js
d84351e451b9fef40fe2532c446aca90d26a4be9038b2d77d3d45dd6eab21d41  node_modules/@earendil-works/pi-agent-core/dist/agent.js
6732a1c65c09577d2ffcb716b48e4f4673e57e3e333f10ebfce5132d82e4d7a2  node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js
db3bfd2ae08eda4936d8807656f06120e6e62672d6a7798bccba486a0dc994ea  dist/core/agent-session.d.ts
b349557f08b25c8655b041ee05d27ff824ffd7c805531ddb460d46b62e4445f7  dist/core/session-manager.d.ts
e968e5be01dc7ad9615f938ae867ef136fa495f13dcf169942e9f781a299d9eb  dist/modes/rpc/rpc-types.d.ts
7caf774e58b6ceab9e4629bde58b9ab332a30278142af5a3e99b316be26030a9  node_modules/@earendil-works/pi-agent-core/dist/agent.d.ts
2ae8602b59de73435b6d9bd7d4c9dd1b344b7f1d1214866bc08feb04fc490061  node_modules/@earendil-works/pi-agent-core/dist/types.d.ts
8c11014ea6c454bf60c7c22b65cdb00bebd834e4e9ebb07d3f0fffb6a58ea78a  node_modules/@earendil-works/pi-ai/dist/types.d.ts
13d6fec97d08f4303714aca50f3113ba0263706e961fc220ccb1cc023c520e6b  node_modules/@earendil-works/pi-ai/dist/api/bedrock-converse-stream.js
SHAS
) || { echo 'Stock Pi bytes changed; refusing patch' >&2; exit 1; }
ROOT=$(mktemp -d /var/tmp/agent-comms-pi-native-XXXXXXXX)
chmod 0700 "$ROOT"
mkdir -p "$ROOT/node_modules/@earendil-works"
TARGET="$ROOT/node_modules/@earendil-works/pi-coding-agent"
cp -a --reflink=auto "$STOCK" "$TARGET"
[[ $(realpath "$TARGET/dist/core/agent-session.js") == "$TARGET/dist/core/agent-session.js" ]] || {
  echo 'Patch target is not an independent copy' >&2; exit 1;
}
patch --batch --fuzz=0 --no-backup-if-mismatch --directory "$TARGET" -p1 < "$SCRIPT_DIR/pi-0.85.1-native-input.patch"
for file in dist/core/agent-session.js dist/core/session-manager.js dist/modes/rpc/rpc-mode.js \
  node_modules/@earendil-works/pi-agent-core/dist/agent.js \
  node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js \
  node_modules/@earendil-works/pi-ai/dist/api/bedrock-converse-stream.js; do
  node --check "$TARGET/$file"
done
printf 'PI_PACKAGE_DIR=%s\nPI_CLI=%s\n' "$TARGET" "$TARGET/dist/cli.js"
