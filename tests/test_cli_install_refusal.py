"""The public CLI reports installer refusals through its declared JSON boundary."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_install_from_non_git_directory_reports_one_json_refusal(tmp_path):
    outside = tmp_path / "non-git"
    outside.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "agent_comms.cli", "--root", str(tmp_path / "bus"), "install"],
        cwd=outside,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 1, (completed.stdout, completed.stderr)
    lines = completed.stdout.splitlines()
    assert len(lines) == 1, (completed.stdout, completed.stderr)
    error = json.loads(lines[0])
    assert error["type"] == "failed"
    assert error["error"].startswith("install: refused:")
    assert "Traceback" not in completed.stdout + completed.stderr
    assert completed.stderr == ""
