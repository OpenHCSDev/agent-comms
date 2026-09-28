"""A live child is visible in the read-only cold-cutover process witness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc witness")
def test_explicit_old_root_child_is_witnessed_without_writing_wire(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, "AGENT_COMMS_ROOT": str(root)},
    )
    try:
        (root / "registry.json").write_text(json.dumps({
            "threads": {"child": {"pid": child.pid}},
        }))
        script = Path(__file__).resolve().parents[1] / "tools/cutover/process_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--root", str(root)],
            check=True, capture_output=True, text=True,
        )
        witness = json.loads(result.stdout)
        matches = [row for row in witness["processes"] if row["pid"] == child.pid]
        assert len(matches) == 1
        assert matches[0]["parent_pid"] == os.getpid()
        assert matches[0]["start_ticks"] > 0
        assert matches[0]["registry_names"] == ["child"]
        assert sorted(root.iterdir()) == [root / "registry.json"]
    finally:
        child.terminate()
        child.wait(timeout=5)
