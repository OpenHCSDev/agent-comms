"""The real SessionStart command works before a commander connects to the bus."""

import json
import os
import subprocess
import sys

import pytest

from agent_comms.commander.config import CommanderConfig


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
@pytest.mark.parametrize("configuration", ["configured", "missing", "invalid"])
def test_session_start_reads_all_packaged_files_without_a_bus(tmp_path, source, configuration):
    home = tmp_path / "home"
    home.mkdir()
    bus = tmp_path / "bus"
    bus.mkdir()
    path = CommanderConfig.path(home)
    if configuration != "missing":
        path.parent.mkdir(parents=True)
        if configuration == "invalid":
            path.write_text("{invalid JSON")
        else:
            config = CommanderConfig(
                commander="fresh-commander",
                root=bus,
                state=tmp_path / "state",
                board=tmp_path / "board.md",
                narrative=tmp_path / "narrative.md",
                repos=(),
                worktrees=tmp_path / "worktrees",
                scratch=tmp_path / "scratch",
                disk=tmp_path,
            )
            path.write_text(config.model_dump_json())
    result = subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.skill"],
        input=json.dumps(
            {"hook_event_name": "SessionStart", "source": source, "session_id": "fresh-session"}
        ),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    notice, files = context.split("\n\n", 1)
    expected_notice = (
        "Commander: fresh-commander."
        if configuration == "configured"
        else "No commander is configured"
    )
    assert notice.startswith(expected_notice)
    sources = (CommanderConfig.packaged_skill(), *CommanderConfig.packaged_rules_files())
    assert files == "\n\n".join(
        f"# Commander file: {file.name}\n{file.read_bytes().decode('utf-8')}" for file in sources
    )
    assert not tuple(bus.iterdir())
