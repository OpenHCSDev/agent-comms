"""Provider-free Pi-settings trigger seam; no source or session write authority."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_comms.owner_compaction_settings import (
    PiSettingsEvidenceError,
    read_compaction_decision,
)

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    not PACKAGE or sys.platform != "linux", reason="Disposable Linux native opt-in"
)


@pytest.fixture
def settings_tree(tmp_path, monkeypatch):
    agent_dir = tmp_path / "private-agent-dir"
    project = tmp_path / "project"
    (project / ".pi").mkdir(parents=True)
    agent_dir.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    global_file = agent_dir / "settings.json"
    project_file = project / ".pi" / "settings.json"
    global_file.write_text(
        json.dumps({"compaction": {"reserveTokens": 100, "keepRecentTokens": 20}})
    )
    project_file.write_text(
        json.dumps({"compaction": {"reserveTokens": 200, "keepRecentTokens": 30}})
    )
    return project, global_file, project_file


def test_selected_pi_settings_strict_trigger_and_read_only(settings_tree):
    project, global_file, project_file = settings_tree
    original = tuple(
        (file.read_bytes(), file.stat().st_mtime_ns, file.stat().st_ctime_ns)
        for file in (global_file, project_file)
    )
    below = read_compaction_decision(
        Path(PACKAGE), str(project), context_tokens=800, context_window=1000
    )
    above = read_compaction_decision(
        Path(PACKAGE), str(project), context_tokens=801, context_window=1000
    )
    assert not below.trigger and above.trigger
    assert above.enabled and above.reserve_tokens == 200 and above.keep_recent_tokens == 30
    assert original == tuple(
        (file.read_bytes(), file.stat().st_mtime_ns, file.stat().st_ctime_ns)
        for file in (global_file, project_file)
    )
    assert not (global_file.parent / "settings.json.lock").exists()
    assert not (project_file.parent / "settings.json.lock").exists()


def test_disabled_and_malformed_settings_fail_closed_without_repair(settings_tree):
    project, global_file, project_file = settings_tree
    project_file.write_text('{"compaction":{"enabled":false}}')
    decision = read_compaction_decision(
        Path(PACKAGE), str(project), context_tokens=990, context_window=1000
    )
    assert decision.enabled is False and decision.trigger is False
    project_file.write_text('{"compaction":')
    before = project_file.read_bytes()
    with pytest.raises(PiSettingsEvidenceError):
        read_compaction_decision(
            Path(PACKAGE), str(project), context_tokens=999, context_window=1000
        )
    assert project_file.read_bytes() == before and global_file.exists()
    assert not (project_file.parent / "settings.json.lock").exists()


def test_symlinked_settings_are_refused_without_following_alias(settings_tree):
    project, global_file, project_file = settings_tree
    project_file.unlink()
    project_file.symlink_to(global_file)
    before = global_file.read_bytes()
    with pytest.raises(PiSettingsEvidenceError):
        read_compaction_decision(
            Path(PACKAGE), str(project), context_tokens=900, context_window=1000
        )
    assert global_file.read_bytes() == before and project_file.is_symlink()


def test_invalid_input_never_launches_settings_reader(settings_tree, monkeypatch):
    from agent_comms import owner_compaction_settings

    project, _, _ = settings_tree
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Invalid evidence must not invoke Pi")

    monkeypatch.setattr(owner_compaction_settings.subprocess, "run", forbidden)
    for tokens, window in [(True, 1000), (-1, 1000), (1, False), (1, 0), (2**53, 1000)]:
        with pytest.raises(PiSettingsEvidenceError):
            read_compaction_decision(
                Path(PACKAGE), str(project), context_tokens=tokens, context_window=window
            )
    assert not calls
