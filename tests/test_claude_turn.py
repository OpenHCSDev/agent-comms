"""Claude turn identity applies equally to complete items and activity."""
from contextlib import nullcontext

import pytest

from agent_comms.turn_phase import ModelWaitPhase
from agent_comms.cli_bridges.claude_protocol import (
    CatchUp, ClaudeSession, KnownTurn, Live, RedactedThinking, TimedRecord,
    ToolUse, UserRecord,
)
from agent_comms.runtime_updates import ActiveItem, FinishedItem
from agent_comms.tool_calls import ToolCall
from agent_comms.transcript_events import AssistantTranscript


@pytest.mark.parametrize('question', ('finished', 'result', 'thinking', 'tool'))
@pytest.mark.parametrize('phase, expectation', (
    (CatchUp, nullcontext()),
    (Live, pytest.raises(ValueError, match='before identifying its turn')),
))
def test_unknown_turn_uses_the_reader_phase_for_every_item(question, phase, expectation):
    session = ClaudeSession(phase=phase())
    record = TimedRecord(uuid='record', timestamp='2026-10-18T00:00:00Z', position=17)
    session.started(ToolCall('call', 'read', {'path': 'a.py'}), record)
    actions = {
        'finished': lambda: session.finished('item', (AssistantTranscript('done', timestamp=record.timestamp, position=17),)),
        'result': lambda: session.tool_result('call', 'done', True, record),
        'thinking': lambda: RedactedThinking().updates(record, session),
        'tool': lambda: ToolUse(id='next', name='Read', input={'file_path': 'a.py'}).updates(record, session),
    }
    with expectation:
        assert actions[question]() == ()


def test_prompt_identifies_finished_and_active_items_with_the_same_turn():
    session = ClaudeSession(phase=Live())
    prompt = UserRecord(uuid='prompt', timestamp='2026-10-18T00:00:00Z', position=17,
                        promptId='turn-id', message={'content': []})
    prompt.updates(session)
    assert session.turn == KnownTurn('turn-id')
    call = ToolCall('call', 'read', {'path': 'a.py'})
    session.started(call, prompt)
    result = TimedRecord(uuid='result', timestamp='2026-10-18T00:00:01Z', position=18)
    completed, active = session.tool_result('call', 'done', True, result)
    assert completed == FinishedItem('turn-id', 'call', (
        call.started(prompt.position, prompt.timestamp),
        call.ended('done', True, result.position, result.timestamp),
    ))
    assert active == ActiveItem('turn-id', 'call', ModelWaitPhase())
