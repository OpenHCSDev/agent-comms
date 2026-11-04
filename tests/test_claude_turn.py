"""Claude turn identity applies equally to complete items and activity."""
from contextlib import nullcontext

import pytest

import json

from transcript_record_fixture import transcript_record

from agent_comms.turn_item import NativeItemId, NativeTurnId
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase
from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.cli_bridges.claude_protocol import (
    CatchUp, ClaudeSession, ClaudeToolDraft, KnownTurn, Live, ReadResultObserved, RedactedThinking,
    TimedRecord, ToolUse, UserRecord,
)
from agent_comms.recorded_time import EventTime
from agent_comms.runtime_updates import ActiveItem, FinishedItem, StartedItem
from agent_comms.tool_calls import ToolCall
from agent_comms.transcript_events import AssistantTranscript


@pytest.mark.parametrize('question', ('finished', 'result', 'thinking', 'tool'))
@pytest.mark.parametrize('phase, expectation', (
    (CatchUp, nullcontext()),
    (Live, pytest.raises(ValueError)),
))
def test_unknown_turn_uses_the_reader_phase_for_every_item(question, phase, expectation):
    session = ClaudeSession(phase=phase())
    record = TimedRecord(uuid=transcript_record('record'), timestamp='2026-10-18T00:00:00Z', position=17)
    call = ToolCall('call', 'read', {'path': 'a.py'})
    session.tools['call'] = ClaudeToolDraft(
        EventTime.capture(record.timestamp), (call.started(record.position, record.timestamp),), call)
    actions = {
        'finished': lambda: session.finished('item', (AssistantTranscript('done', timestamp=record.timestamp, position=17),)),
        'result': lambda: session.tool_result('call', 'done', True, record),
        'thinking': lambda: RedactedThinking().updates(record, session),
        'tool': lambda: ToolUse(id='next', name='Read', input={'file_path': 'a.py'}).updates(record, session),
    }
    with expectation:
        assert actions[question]() == ()


def test_prompt_identifies_finished_and_active_items_with_the_same_turn():
    turn_id_turn_id = NativeTurnId(transcript_record('prompt'))
    session = ClaudeSession(phase=Live())
    prompt = UserRecord(uuid=transcript_record('prompt'), timestamp='2026-10-18T00:00:00Z', position=17,
                        promptId='turn-id', message={'content': []})
    prompt.updates(session)
    assert session.turn == KnownTurn(turn_id_turn_id)
    (started,) = ToolUse(id='call', name='Read', input={'file_path': 'a.py'}).updates(prompt, session)
    assert isinstance(started, StartedItem)
    assert started.phase == ToolRunningPhase(tools=(('call', started.accumulator.call.title),))
    call = started.accumulator.call
    result = TimedRecord(uuid=transcript_record('result'), timestamp='2026-10-18T00:00:01Z', position=18)
    completed, active, read = session.tool_result('call', 'done', True, result)
    assert isinstance(read, ReadResultObserved) and read.source_turn.require_id() == turn_id_turn_id
    assert completed == FinishedItem(turn_id_turn_id, NativeItemId('call'), (
        call.started(prompt.position, prompt.timestamp),
        call.ended('done', True, result.position, result.timestamp),
    ))
    assert active == ActiveItem(turn_id_turn_id, NativeItemId('call'), ModelWaitPhase())


def test_scheduled_wake_after_turn_end_opens_the_turn_its_thinking_belongs_to():
    """A scheduled-task fire is a meta prompt without an origin; its turnOrigin opens the turn."""
    session = ClaudeSession(phase=Live())
    records = (
        dict(type='system', subtype='turn_duration'),
        dict(type='system', subtype='scheduled_task_fire', uuid=transcript_record('fire'),
             timestamp='2026-10-30T06:57:12.685Z', content='Running scheduled task'),
        dict(type='user', uuid=transcript_record('wake'), timestamp='2026-10-30T06:57:13.123Z',
             sessionId='session', promptId='scheduled-prompt', isMeta=True, turnOrigin='scheduled',
             promptSource='system', message=dict(content='OVERNIGHT SWEEP')),
        dict(type='assistant', uuid=transcript_record('thinking'), timestamp='2026-10-30T06:57:14.899Z',
             message=dict(content=[dict(type='thinking', thinking='Read the board')])),
    )
    for position, data in enumerate(records):
        ClaudeJsonLines().decode_line(json.dumps(data).encode(), position).updates(session)
    assert session.turn == KnownTurn(NativeTurnId(transcript_record('wake')))


def compaction_after(session, *records):
    """The records Claude wrote around a compaction, read as the live bridge reads them."""
    boundary = dict(type='system', subtype='compact_boundary', uuid=transcript_record('boundary'),
                    timestamp='2026-11-04T10:44:04.000Z', content='Conversation compacted')
    for position, data in enumerate(records):
        ClaudeJsonLines().decode_line(json.dumps(data).encode(), position).updates(session)
    return ClaudeJsonLines().decode_line(json.dumps(boundary).encode(), len(records)).updates(session)


def test_a_compaction_between_turns_opens_the_turn_of_its_notice():
    """2026-11-04: Claude compacted 55 minutes after a turn_duration; the live reader's raise
    stopped the bridge, and every tool call of that session was refused until it restarted."""
    session = ClaudeSession(phase=Live())
    (finished,) = compaction_after(session, dict(type='system', subtype='turn_duration'))
    opened = NativeTurnId(transcript_record('boundary'))
    assert session.turn == KnownTurn(opened)
    assert isinstance(finished, FinishedItem) and finished.turn_id == opened


def test_a_compaction_inside_a_turn_stays_in_that_turn():
    running = NativeTurnId(transcript_record('prompt'))
    session = ClaudeSession(phase=Live(), turn=KnownTurn(running))
    (finished,) = compaction_after(session)
    assert session.turn == KnownTurn(running) and finished.turn_id == running


def test_an_assistant_record_with_no_prompt_opens_its_own_turn():
    """2026-11-04: Claude answered a queued notification with no prompt record right after a
    turn_duration; the live reader's raise stopped the bridge, and no message reached the session."""
    session = ClaudeSession(phase=Live())
    records = (
        dict(type='system', subtype='turn_duration'),
        dict(type='assistant', uuid=transcript_record('thinking'), timestamp='2026-11-04T20:14:11.381Z',
             message=dict(content=[dict(type='thinking', thinking='Read the message')])),
    )
    for position, data in enumerate(records):
        ClaudeJsonLines().decode_line(json.dumps(data).encode(), position).updates(session)
    assert session.turn == KnownTurn(NativeTurnId(transcript_record('thinking')))
