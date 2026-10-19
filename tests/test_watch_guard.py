"""A Claude Code session hears about a lapsed bridge watch from its own hooks."""
import json
import subprocess
import sys

from agent_comms.harness_install import ClaudeCodeHarness
from agent_comms_watch import WatchClaim, WatchRecord


def test_only_a_lapsed_watch_warns_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    record = WatchRecord('claude-code', '/bus', ('agent-comms-claude-bridge', 'watch', '--state', '/s'))

    def hook(event='PostToolUse', session='session'):
        stdin = json.dumps({'session_id': session, 'hook_event_name': event})
        return subprocess.run([sys.executable, '-c', 'from agent_comms_watch import main; main()'],
                              input=stdin, capture_output=True, text=True, check=True).stdout

    assert hook() == ''
    with WatchClaim('session').held(record):
        assert hook() == ''
    lapsed = json.loads(hook('UserPromptSubmit'))['hookSpecificOutput']
    assert lapsed['hookEventName'] == 'UserPromptSubmit'
    assert 'claude-code' in lapsed['additionalContext']
    assert 'agent-comms-claude-bridge watch --state /s' in lapsed['additionalContext']
    assert hook(session='other') == ''

    ClaudeCodeHarness(tmp_path).configure()
    ClaudeCodeHarness(tmp_path).configure()
    hooks = json.loads((tmp_path / '.claude/settings.json').read_text())['hooks']
    assert {event: [hook['command'] for entry in entries for hook in entry['hooks']]
            for event, entries in hooks.items()} == {
        'PreToolUse': ['agent-comms-commander send-guard'],
        'UserPromptSubmit': ['agent-comms-watch-guard'],
        'PostToolUse': ['agent-comms-watch-guard']}
