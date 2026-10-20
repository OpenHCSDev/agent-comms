"""Installing Claude Code writes exactly the declared hook programs, once each."""
import json
import shlex
import tomllib
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_comms_hooks
import agent_comms.harness_install as harness_install
from agent_comms.harness_install import ClaudeCodeHarness


@pytest.fixture
def declared_scripts(monkeypatch):
    # Use this checkout's declarations for both hook commands and package ownership.
    scripts = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text())['project']['scripts']
    points = EntryPoints(EntryPoint(name, value, 'console_scripts') for name, value in scripts.items())
    monkeypatch.setattr(agent_comms_hooks, 'entry_points', lambda group: points.select(group=group))
    monkeypatch.setattr(harness_install, 'distribution', lambda name: SimpleNamespace(entry_points=points))
    return scripts


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
    assert 'matcher' not in hooks['PostToolUse'][0]
    assert 'matcher' not in hooks['SessionStart'][0]


@pytest.mark.parametrize('target', ['agent_comms.commander.cli:main', 'agent_comms.native_pi:main'])
@pytest.mark.parametrize('absolute', [False, True])
def test_configure_replaces_stale_owned_hooks_and_preserves_foreign_hooks(
    tmp_path, declared_scripts, target, absolute,
):
    (script,) = (name for name, value in declared_scripts.items() if value == target)
    executable = str(tmp_path / 'old install' / 'bin' / script) if absolute else script
    stale = {'type': 'command', 'command': shlex.join((executable, 'order', 'peer-sync'))}
    foreign = {'type': 'command', 'command': 'agent-comms-foreign --run', 'timeout': 17}
    prompt = {'type': 'prompt', 'prompt': 'Check the repository'}
    programs = agent_comms_hooks.ClaudeHookProgram.members()
    current = programs[0]
    event = current.events[0]
    mixed = {'matcher': 'Read', 'hooks': [stale, foreign, *current.entry()['hooks']]}
    foreign_prompt = {'matcher': '*', 'hooks': [prompt]}
    empty_foreign = {'matcher': 'Write', 'hooks': []}
    settings = tmp_path / '.claude/settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        'unrelated': {'preserve': True},
        'hooks': {
            event: [mixed, current.entry(), empty_foreign],
            'Stop': [{'hooks': [stale]}, foreign_prompt, current.entry()],
            'UserPromptSubmit': [{'hooks': [stale]}],
            'PreToolUse': [{'hooks': [stale]}],
        },
    }))
    assert ClaudeCodeHarness(tmp_path).configure() == (settings,)
    configured = settings.read_bytes()
    document = json.loads(configured)
    hooks = document['hooks']
    assert document['unrelated'] == {'preserve': True}
    assert hooks[event][0] == {'matcher': 'Read', 'hooks': [foreign]}
    assert empty_foreign in hooks[event]
    assert hooks['Stop'] == [foreign_prompt]
    assert hooks['UserPromptSubmit'] == []
    assert hooks['PreToolUse'] == []
    commands = {name: [hook['command'] for entry in entries for hook in entry['hooks']
                       if hook['type'] == 'command'] for name, entries in hooks.items()}
    for name, actual in commands.items():
        expected = [program.command() for program in programs if name in program.events]
        if name == event:
            expected.insert(0, foreign['command'])
        assert actual == expected
        assert stale['command'] not in actual
    ClaudeCodeHarness(tmp_path).configure()
    assert settings.read_bytes() == configured
