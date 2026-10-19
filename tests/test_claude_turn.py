"""Claude turn identity applies equally to complete items and activity."""
from contextlib import nullcontext

import pytest

from agent_comms.activity import ActivityState
from agent_comms.cli_bridges.claude_protocol import (
    CatchUp, ClaudeSession, KnownTurn, Live, RedactedThinking, TimedRecord,
    ToolUse, UserRecord,
)
from agent_comms.cli_bridges.shared import ActiveItem, FinishedItem, ToolCall
from agent_comms.transcript_events import AssistantTranscript


@pytest.mark.parametrize('question', ('finished', 'result', 'thinking', 'tool'))
@pytest.mark.parametrize('phase, expectation', (
    (CatchUp, nullcontext()),
    (Live, pytest.raises(ValueError, match='before identifying its turn')),
))
def test_unknown_turn_uses_the_reader_phase_for_every_item(question, phase, expectation):
    session = ClaudeSession(phase=phase())
    record = TimedRecord(uuid='record', timestamp='2026-10-18T00:00:00Z', position=17)
    session.started(ToolCall('call', 'read', {'path': 'a.py'}))
    actions = {
        'finished': lambda: session.finished('item', (AssistantTranscript('done', position=17),)),
        'result': lambda: session.tool_result('call', 'done', True, record.position),
        'thinking': lambda: RedactedThinking().updates(record, session),
        'tool': lambda: ToolUse(id='next', name='Read', input={'file_path': 'a.py'}).updates(record, session),
    }
    with expectation:
        assert actions[question]() == ()


def test_prompt_identifies_finished_and_active_items_with_the_same_turn():
    session = ClaudeSession(phase=Live())
    UserRecord(uuid='prompt', timestamp='2026-10-18T00:00:00Z', position=17,
               promptId='turn-id', message={'content': []}).updates(session)
    assert session.turn == KnownTurn('turn-id')
    session.started(ToolCall('call', 'read', {'path': 'a.py'}))
    completed, active = session.tool_result('call', 'done', True, 18)
    assert completed == FinishedItem('turn-id', 'call', ToolCall('call', 'read', {'path': 'a.py'}).finished('done', True, 18))
    assert active == ActiveItem('turn-id', 'call', ActivityState.WORKING, '')
