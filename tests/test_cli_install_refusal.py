"""The public CLI reports installer refusals through its declared JSON boundary."""
from __future__ import annotations

import json

from cli_command import core


def test_install_from_non_git_directory_reports_one_json_refusal(tmp_path, monkeypatch):
    outside = tmp_path / "non-git"
    outside.mkdir()
    monkeypatch.chdir(outside)
    completed = core("--root", str(tmp_path / "bus"), "install")
    assert completed.returncode == 1, (completed.stdout, completed.stderr)
    lines = completed.stdout.splitlines()
    assert len(lines) == 1, (completed.stdout, completed.stderr)
    error = json.loads(lines[0])
    assert error["type"] == "failed"
    assert error["error"].startswith("install: refused:")
    assert "Traceback" not in completed.stdout + completed.stderr
