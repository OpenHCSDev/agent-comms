"""Observed Claude Code records decode through the transcript's declared union."""

from pathlib import Path

import pytest

from agent_comms.cli_bridges.claude_protocol import (
    CLAUDE_RECORD,
    BridgeStatus,
    ClaudeSession,
    Relocated,
    SystemRecord,
    WorktreeState,
)


def test_bridge_status_system_record():
    fixture = Path(__file__).parent / 'fixtures' / 'claude' / 'bridge_status.json'
    record = CLAUDE_RECORD.validate_json(fixture.read_bytes())

    assert record == SystemRecord.model_construct(
        cwd='/workspace/project',
        version='2.1.310',
        subtype=BridgeStatus(
            content='/remote-control is active · Continue here, on your phone, or at '
                    'https://claude.ai/code/session_bridge_status_fixture',
            url='https://claude.ai/code/session_bridge_status_fixture',
        ),
    )
    assert record.updates(ClaudeSession()) == ()
    assert not record.begins_history()


@pytest.mark.parametrize(('fields', 'kind'), (
    ({'type': 'relocated', 'sessionId': 'session', 'relocatedCwd': '/workspace/worktree'}, Relocated),
    ({'type': 'worktree-state', 'sessionId': 'session'}, WorktreeState),
))
def test_entering_a_worktree_leaves_records_the_bridge_reads(fields, kind):
    """2026-10-30: EnterWorktree's records stopped the bridge at every reconnect, for good,
    because they stay in the transcript."""
    record = CLAUDE_RECORD.validate_python({**fields, 'position': 0})

    assert isinstance(record, kind)
    assert record.updates(ClaudeSession()) == ()
    assert not record.begins_history()
