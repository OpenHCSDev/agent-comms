"""A Claude Code session hears about a lapsed bridge watch from its own hooks."""
import json
import subprocess
import sys
import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path

import agent_comms_hooks
from agent_comms.harness_install import ClaudeCodeHarness
from agent_comms_hooks.watch import WatchClaim, WatchRecord


def test_only_a_lapsed_watch_warns_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    record = WatchRecord('claude-code', '/bus', ('agent-comms-claude-bridge', 'watch', '--state', '/s'))

    def hook(event='PostToolUse', session='session'):
        stdin = json.dumps({'session_id': session, 'hook_event_name': event})
        return subprocess.run([sys.executable, '-c', 'from agent_comms_hooks.watch import main; main()'],
                              input=stdin, capture_output=True, text=True, check=True).stdout

    assert hook() == ''
    with WatchClaim('session').held(record), WatchClaim('session').reader_held():
        assert hook() == ''
    lapsed = json.loads(hook('UserPromptSubmit'))['hookSpecificOutput']
    assert lapsed['hookEventName'] == 'UserPromptSubmit'
    assert 'claude-code' in lapsed['additionalContext']
    assert 'agent-comms-claude-bridge watch --state /s' in lapsed['additionalContext']
    assert hook(session='other') == ''

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
    assert hooks['PreToolUse'][0]['matcher'] == 'Bash'
    assert 'matcher' not in hooks['PostToolUse'][0]
    assert 'matcher' not in hooks['SessionStart'][0]

