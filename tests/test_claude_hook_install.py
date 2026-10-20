"""Installing Claude Code replaces its writer-tagged groups, not command names."""
import json
import tomllib
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path

import pytest

import agent_comms_hooks
from agent_comms.harness_install import ClaudeCodeHarness


@pytest.fixture
def declared_scripts(monkeypatch):
    scripts = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text())['project']['scripts']
    points = EntryPoints(EntryPoint(name, value, 'console_scripts') for name, value in scripts.items())
    monkeypatch.setattr(agent_comms_hooks, 'entry_points', lambda group: points.select(group=group))


def test_install_writes_each_declared_hook_program_once(tmp_path, declared_scripts):
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
    assert all(agent_comms_hooks.ClaudeHookProgram.owns_entry(entry)
               for entries in hooks.values() for entry in entries)
    assert 'matcher' not in hooks['PostToolUse'][0]
    assert 'matcher' not in hooks['SessionStart'][0]


def test_configure_removes_tagged_deleted_program_and_preserves_foreign_groups(
    tmp_path, declared_scripts,
):
    programs = agent_comms_hooks.ClaudeHookProgram.members()
    current = programs[0]
    event = current.events[0]
    deleted = tmp_path / 'agent-comms-watch-guard'
    deleted.write_text('#!/bin/sh\nexit 0\n')
    stale = current.entry()
    stale['hooks'][0]['command'] = str(deleted)
    deleted.unlink()
    foreign = {'writer': 'another-tool', 'matcher': '*',
               'hooks': [{'type': 'command', 'command': 'foreign-tool', 'timeout': 17}]}
    untagged = {'matcher': 'Read', 'hooks': [
        {'type': 'command', 'command': 'agent-comms-foreign --run'}]}
    settings = tmp_path / '.claude/settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        'unrelated': {'preserve': True},
        'hooks': {
            event: [stale, untagged, current.entry()],
            'Stop': [stale, foreign],
            'UserPromptSubmit': [stale],
            'PreToolUse': [stale],
        },
    }))
    assert not deleted.exists()
    assert ClaudeCodeHarness(tmp_path).configure() == (settings,)
    configured = settings.read_bytes()
    document = json.loads(configured)
    hooks = document['hooks']
    assert document['unrelated'] == {'preserve': True}
    assert hooks[event][0] == untagged
    assert hooks['Stop'] == [foreign]
    assert hooks['UserPromptSubmit'] == []
    assert hooks['PreToolUse'] == []
    for name, entries in hooks.items():
        owned = [entry for entry in entries if agent_comms_hooks.ClaudeHookProgram.owns_entry(entry)]
        assert owned == [program.entry() for program in programs if name in program.events]
        assert all(hook.get('command') != str(deleted) for entry in entries for hook in entry['hooks'])
    ClaudeCodeHarness(tmp_path).configure()
    assert settings.read_bytes() == configured
