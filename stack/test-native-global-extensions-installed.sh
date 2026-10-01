#!/usr/bin/env bash
# Actual globals + saved RPC using only the supplied installed Core prefix.
set -euo pipefail
if [[ $# -lt 3 ]]; then
    printf 'Usage: %s INSTALLED_PREFIX NATIVE_PACKAGE NEW_FIXTURE [--session ORIGINAL_SESSION]\n' "$0" >&2
    exit 2
fi
installed_prefix=$1
native_package=$2
fixture=$3
shift 3
stack_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
unset PYTHONPATH PYTHONHOME
"$installed_prefix/bin/python" -I - "$installed_prefix" "$native_package" <<'PYCODE'
import pathlib, sys
import agent_comms
import agent_comms.native_package as resource
from agent_comms.native_pi import _trusted_package
prefix = pathlib.Path(sys.argv[1]).resolve(strict=True)
module = pathlib.Path(agent_comms.__file__).resolve(strict=True)
if not module.is_relative_to(prefix):
    raise SystemExit(f"Core did not import from installed prefix: {module}")
if pathlib.Path(sys.prefix).resolve(strict=True) != prefix:
    raise SystemExit("Interpreter prefix differs from declared installed prefix")
_trusted_package(pathlib.Path(sys.argv[2]))
print(f"Installed Core: {module}; native manifest: {resource.MANIFEST}", file=sys.stderr)
PYCODE
exec "$installed_prefix/bin/python" -I "$stack_root/test-native-global-extensions.py" \
    "$native_package" "$fixture" "$@"
