"""Installing Claude Code writes exactly the declared hook programs, once each."""
import json
import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path

import agent_comms_hooks
from agent_comms.harness_install import ClaudeCodeHarness


def test_install_writes_each_declared_hook_program_once(tmp_path, monkeypatch):
    # The console scripts this checkout installs, so the commands come from its own pyproject.
    scripts = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text())['project']['scripts']
    monkeypatch.setattr(agent_comms_hooks, 'entry_points', lambda group: tuple(
        EntryPoint(name, value, group) for name, value in scripts.items()))
    ClaudeCodeHarness(tmp_path).configure()
    ClaudeCodeHarness(tmp_path).configure()
    hooks = json.loads((tmp_path / '.claude/settings.json').read_text())['hooks']
    programs = agent_comms_hooks.ClaudeHookProgram.members()
    expected = {event: {program.command() for program in programs if event in program.events}
                for program in programs for event in program.events}
    actual = {event: [hook['command'] for entry in entries for hook in entry['hooks']]
              for event, entries in hooks.items()}
    assert {event: set(commands) for event, commands in actual.items()} == expected
    assert all(len(commands) == len(set(commands)) for commands in actual.values())
    assert 'matcher' not in hooks['PostToolUse'][0]
    assert 'matcher' not in hooks['SessionStart'][0]
