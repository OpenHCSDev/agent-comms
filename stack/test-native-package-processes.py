#!/usr/bin/env python3
"""Run the package subprocess probe behind the native fence and kernel network denial."""

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
    package = Path(os.environ["PI_NATIVE_PACKAGE_DIR"]).resolve(strict=True)
    node = shutil.which("node")
    assert node
    deny = runpy.run_path(str(stack / "test-native-import-rpc.py"))["network_denial"]()
    with tempfile.TemporaryDirectory(prefix="native-package-processes-") as directory:
        root = Path(directory)
        (root / "home").mkdir()
        environment = {
            "PATH": os.defpath,
            "HOME": str(root / "home"),
            "TMPDIR": str(root),
            "PI_OFFLINE": "0",
            "PI_SKIP_VERSION_CHECK": "1",
            "NODE_DISABLE_COMPILE_CACHE": "1",
        }
        result = subprocess.run(
            [node, "--no-global-search-paths", "--import",
             str(package / "dist/agent-comms-import-fence.mjs"),
             "--input-type=module", "--eval",
             (stack / "probe-native-package-processes.mjs").read_text(),
             str(package), str(root)],
            cwd=root, env=environment, preexec_fn=deny,
            capture_output=True, text=True, timeout=30,
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        assert result.returncode == 0, f"fixture exit {result.returncode}"


if __name__ == "__main__":
    main()
