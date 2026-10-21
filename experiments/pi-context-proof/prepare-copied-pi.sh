#!/usr/bin/env bash
# Reproducible, opt-in native-input fork. NEVER patches the installed Pi.
set -euo pipefail
unset NODE_OPTIONS NODE_PATH
STOCK=${PI_STOCK_DIR:?PI_STOCK_DIR must name the package fetched by prepare-pi-native}
[[ $# -eq 1 && -n "$1" ]] || { echo 'Usage: prepare-copied-pi.sh PINNED_NPM_SPEC' >&2; exit 2; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
node -e '
const stock = require(process.argv[1]);
const actual = `${stock.name}@${stock.version}`;
if (actual !== process.argv[2]) {
  console.error(`Stock package identity differs: expected ${process.argv[2]}, got ${actual}`);
  process.exit(1);
}
' "$STOCK/package.json" "$1"
ROOT=$(mktemp -d "${TMPDIR:-/var/tmp}/agent-comms-pi-native-XXXXXXXX")
chmod 0700 "$ROOT"
mkdir -p "$ROOT/node_modules/@earendil-works"
TARGET="$ROOT/node_modules/@earendil-works/pi-coding-agent"
# Materialize only internal regular-file aliases (npm's .bin launchers). Never
# leave a copied symlink capable of redirecting a later patch into stock files.
python3 - "$STOCK" "$TARGET" <<'PY'
import shutil
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve(strict=True)
for index, path in enumerate(root.rglob("*")):
    if index >= 30000:
        raise SystemExit("Stock package inventory limit exceeded")
    if path.is_symlink():
        target = path.resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise SystemExit("Stock package contains an external or directory alias")
shutil.copytree(root, sys.argv[2], symlinks=False)
PY
patch --batch --fuzz=0 --no-backup-if-mismatch --directory "$TARGET" -p1 < "$SCRIPT_DIR/native-input.patch"
for file in dist/core/agent-session.js dist/core/session-manager.js dist/modes/rpc/rpc-mode.js \
  node_modules/@earendil-works/pi-agent-core/dist/agent.js \
  node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js \
  node_modules/@earendil-works/pi-ai/dist/api/bedrock-converse-stream.js; do
  node --check "$TARGET/$file"
done
printf 'PI_PACKAGE_DIR=%s\nPI_CLI=%s\n' "$TARGET" "$TARGET/dist/cli.js"
