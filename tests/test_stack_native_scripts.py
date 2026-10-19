"""Every stack/test-native-* script runs against the pinned native package, found on disk."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[1] / "stack"
INTERPRETERS = {".mjs": (shutil.which("node") or "node",), ".py": (sys.executable,), ".sh": ("bash",)}


@pytest.mark.parametrize("script", sorted(STACK.glob("test-native-*")), ids=lambda path: path.name)
def test_stack_native_scripts_pass_against_the_pinned_package(script, native_runtime_package, tmp_path):
    environment = dict(os.environ, PI_NATIVE_PACKAGE_DIR=str(native_runtime_package.path), TMPDIR=str(tmp_path))
    run = subprocess.run((*INTERPRETERS[script.suffix], str(script)), cwd=STACK.parent, env=environment,
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
