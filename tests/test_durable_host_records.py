"""Durable host uses the shared Pi command boundary and required context proof."""
import asyncio
import json

import pytest

from agent_comms.agent_backend import ContextUsage, FailedCompaction, NothingToCompaction, PlacedCompaction, StaleCompaction
from agent_comms.agent_events import Chunk, Thinking, ToolStart, Error
from agent_comms.pi_durable_backend import (
    DurableDatabase, DurableDone, DurablePiEvent, Inspect, PiDurableBackend, Settle,
)
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.pi_commands import Compact, GetSessionStats
from agent_comms.pi_payloads import PiContextUsage


def decode(wire):
    return PiRpcChannel.decode_record((json.dumps(wire) + '\n').encode(), strict=True, event_type=DurablePiEvent)


@pytest.mark.parametrize('result,expected', [
    ({}, NothingToCompaction()),
    ({'summary': 'summary', 'firstKeptEntryId': '7', 'tokensBefore': 30},
     PlacedCompaction('summary', '7', 30)),
    ({'stale': True}, StaleCompaction()),
])
def test_compaction_reply_uses_shared_pi_outcome(result, expected):
    decoded = decode({'type': 'response', 'command': 'compact', 'success': True,
                      'id': '7', 'data': result})
    assert decoded.command is Compact
    assert decoded.data.outcome == expected


@pytest.mark.parametrize('event,expected', [
    ({'type': 'message_update', 'usage': {}, 'assistantMessageEvent':
        {'type': 'text_delta', 'contentIndex': 0, 'delta': 'visible'}}, (Chunk('visible'),)),
    ({'type': 'message_update', 'usage': {}, 'assistantMessageEvent':
        {'type': 'thinking_delta', 'contentIndex': 1, 'delta': 'reasoning'}}, (Thinking('reasoning'),)),
    ({'type': 'tool_execution_start', 'toolCallId': 'call', 'toolName': 'read',
      'args': {'path': 'source.py'}},
     (ToolStart(id='call', name='read', title='Read source.py', kind='read', args={'path': 'source.py'}),)),
    ({'type': 'task_failed', 'taskId': 3, 'kind': 'tool', 'message': 'failed'}, (Error('failed'),)),
])
def test_event_fields_decode_at_frame_ingress(event, expected):
    assert decode(event).convert() == expected


def test_success_requires_proof_and_failure_is_a_different_event():
    with pytest.raises((TypeError, ValueError), match='proof'):
        decode({'type': 'durable_turn_end', 'text': 'answer'})
    with pytest.raises((TypeError, ValueError)):
        decode({'type': 'durable_turn_end', 'text': 'answer', 'proof': None})
    failed = decode({'type': 'durable_turn_failed', 'text': '', 'reason': 'model_error'})
    assert all(not isinstance(event, DurableDone) for event in failed.convert())
    assert failed.convert()[-1].ok is False


def test_real_host_context_and_reopen_keep_one_database(tmp_path, thread_registration):
    async def journey():
        backend = PiDurableBackend(tmp_path)
        location = DurableDatabase(str(tmp_path / 'records.sqlite3'))
        thread = thread_registration.new_thread('records', frozenset(), str(tmp_path), backend=PiDurableBackend,
                        session_file=location.path)
        try:
            outcome = await backend.compact(thread, 'retain the empty plan')
            assert isinstance(outcome, FailedCompaction)
            assert 'model' in outcome.message.lower()
            first = await backend.open(thread)
            assert first.location == location
            identity = (await first.request(Inspect())).work
            await backend.close_idle()
            second, third = await asyncio.gather(backend.open(thread), backend.open(thread))
            assert second is third and second.location == location
            assert await backend.context_usage(thread) == ContextUsage(0, None)
            assert (await second.request(Inspect())).work == identity
            with pytest.raises(RuntimeError, match='Recovery is only available before resume'):
                await second.request(Settle('task:missing', 'ABANDON'))
            assert (await second.request(GetSessionStats())).context_usage == PiContextUsage(0, None)
            assert second.proc.alive()
        finally:
            await backend.close_idle()
    asyncio.run(journey())
