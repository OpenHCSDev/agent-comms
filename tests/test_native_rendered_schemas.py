"""Committed native schemas stay byte-identical to their owning declarations."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("schema", ["proof", "file_artifact"])
def test_rendered_schema_matches_declarations(tmp_path, schema):
    target = tmp_path / "schema.mjs"
    subprocess.run(
        [sys.executable, str(ROOT / f"tools/render_native_{schema}_schema.py"), str(target)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=True,
    )
    committed = ROOT / f"stack/native/native-{schema.replace('_', '-')}-schema.mjs"
    assert target.read_bytes() == committed.read_bytes()
