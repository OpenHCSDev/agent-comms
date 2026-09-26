#!/usr/bin/env python3
"""Provider-free, kernel-network-denied old/new package-process admission probe."""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    stack = Path(__file__).resolve().parent
    package = Path(sys.argv[1]).resolve(strict=True)
    mode = sys.argv[2] if len(sys.argv) > 2 else "new"
    assert mode in {"old", "new"}
    node = shutil.which("node")
    assert node
    root = Path(tempfile.mkdtemp(prefix="pr95-package-processes-", dir="/var/tmp"))
    (root / "home").mkdir()
    environment = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "TMPDIR": "/var/tmp",
        "PI_OFFLINE": "0",
        "PI_SKIP_VERSION_CHECK": "1",
        "NODE_DISABLE_COMPILE_CACHE": "1",
    }
    deny = runpy.run_path(str(stack / "test-native-import-rpc.py"))["network_denial"]()
    result = subprocess.run(
        [
            node,
            "--no-global-search-paths",
            "--import",
            str(package / "dist/agent-comms-import-fence.mjs"),
            "--input-type=module",
            "--eval",
            (stack / "test-native-package-processes.mjs").read_text(),
            str(package),
            str(root),
            mode,
        ],
        cwd=root,
        env=environment,
        preexec_fn=deny,
        capture_output=True,
        text=True,
        timeout=30,
    )
    (root / "result.log").write_text(result.stdout + result.stderr)
    print(f"fixture={root}")
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    assert result.returncode == 0, f"fixture exit {result.returncode}"


if __name__ == "__main__":
    main()
