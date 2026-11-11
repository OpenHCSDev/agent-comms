"""Claude turn identity applies equally to complete items and activity."""
import json
from contextlib import nullcontext

import pytest

from agent_comms.cli_bridges.claude_protocol import (
    ApiError,
    CatchUp,
    ClaudeSession,
    ClaudeToolDraft,
    KnownTurn,
    Live,
    ReadResultObserved,
    RedactedThinking,
    TimedRecord,
    ToolUse,
    UnmodelledSystemEvent,
    UserRecord,
)
from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.cli_bridges.runtime_updates import ActiveItem, FinishedItem, StartedItem
from agent_comms.cli_bridges.tool_calls import ToolCall
from agent_comms.execution.turn_item import (
    NativeItemId,
    NativeTurnId,
)
from agent_comms.execution.turn_phase import (
    ModelWaitPhase,
    ToolRunningPhase,
)
from agent_comms.storage.recorded_time import EventTime
from agent_comms.transcript.transcript_events import AssistantTranscript
from transcript_record_fixture import transcript_record


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
    assert completed == FinishedItem(turn_id=turn_id_turn_id, item_id=NativeItemId('call'), events=(
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


@pytest.mark.parametrize('notice', [
    # 2026-11-06: Claude Code 2.1.318's first api_error record (a 529 retry notice) stopped
    # claude-b-ui's watch; nothing reached the bus after it.
    dict(type='system', subtype='api_error', level='error', uuid=transcript_record('retry'),
         timestamp='2026-11-06T19:53:37.000Z', sessionId='session', source='request_retry',
         retryAttempt=1, retryInMs=501, maxRetries=7, version='2.1.318',
         error=dict(message='529 overloaded_error', status=529, requestId='req_1',
                    formatted='529 Overloaded', connection=None, isNetworkDown=False,
                    rateLimits=None, noResponse=None)),
    # 2026-11-08: a refused request retried on the fallback model stopped claude-laptop's
    # bridge, and every restart died on the same line.
    dict(type='system', subtype='model_refusal_fallback', level='warning',
         uuid=transcript_record('fallback'), timestamp='2026-11-06T21:58:52.752Z',
         sessionId='session', content='Switched to the fallback model.', trigger='refusal',
         direction='retry', scope='session', originalModel='claude-opus-5-5',
         fallbackModel='claude-opus-5', requestId='req_2', apiRefusalCategory='reasoning_extraction',
         apiRefusalExplanation=None, refusedUserMessageUuid=None, isMeta=False),
], ids=lambda notice: notice['subtype'])
def test_a_retry_notice_reads_as_a_system_event_that_ends_no_turn(notice):
    running = NativeTurnId(transcript_record('prompt'))
    session = ClaudeSession(phase=Live(), turn=KnownTurn(running))
    record = ClaudeJsonLines().decode_line(json.dumps(notice).encode(), 0)
    assert not record.ends_turn()
    assert record.updates(session) == ()
    assert session.turn == KnownTurn(running)

def test_a_subtype_this_build_never_heard_of_keeps_the_session_alive():
    """Claude Code owns this vocabulary and extends it without warning. Twice in two days an
    undeclared subtype took a whole session's delivery down instead of costing one unread
    notice: api_error stopped claude-b-ui's watch on 2026-11-06, and model_refusal_fallback
    stopped claude-laptop's bridge on 2026-11-08 on every reconnect.
    """
    running = NativeTurnId(transcript_record('prompt'))
    session = ClaudeSession(phase=Live(), turn=KnownTurn(running))
    unheard = dict(
        type='system', subtype='a_subtype_claude_adds_next_week', level='info',
        uuid=transcript_record('unheard'), timestamp='2026-11-08T00:00:00.000Z',
        sessionId='session', content='something a newer build explains',
    )
    record = ClaudeJsonLines().decode_line(json.dumps(unheard).encode(), 0)

    assert isinstance(record.subtype, UnmodelledSystemEvent)
    assert not record.ends_turn()
    assert record.updates(session) == ()
    assert session.turn == KnownTurn(running), 'an unmodelled notice moves nothing'

    # A subtype we do model still answers for itself, so this is a floor and not a funnel.
    declared = dict(unheard, subtype='api_error', uuid=transcript_record('declared'))
    assert type(ClaudeJsonLines().decode_line(
        json.dumps(declared).encode(), 1).subtype) is ApiError


