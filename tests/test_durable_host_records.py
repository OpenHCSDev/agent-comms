"""Typed durable JSONL ingress and real-host replies, without provider work."""
import asyncio
import json

import pytest
from agent_comms.field_codec import MalformedRecord

from agent_comms.agent_backend import (
    FailedCompaction,
    NothingToCompaction,
    PlacedCompaction,
    StaleCompaction,
)
from agent_comms.agent_events import Chunk, Error, Thinking, ToolStart
from agent_comms.pi_commands import GetSessionStats
from agent_comms.pi_counts import PiCount, UnreportedCount
from agent_comms.pi_durable_backend import (
    CompactDurable,
    DurableDatabase,
    DurablePiEvent,
    Inspect,
    PiDurableBackend,
    Settle,
)
from agent_comms.pi_events import PiEvent, UnknownPiEvent
from agent_comms.pi_payloads import PiContextUsage
from agent_comms.pi_rpc import PiRpcChannel


def decode(wire):
    return PiRpcChannel.decode_record((json.dumps(wire) + '\n').encode(), strict=True, event_type=DurablePiEvent)


@pytest.mark.parametrize('result,expected', [
    ({'kind': 'nothing_to'}, NothingToCompaction()),
    ({'kind': 'placed', 'summary': 'summary', 'first_kept': '7', 'tokens_before': 30},
     PlacedCompaction('summary', '7', PiCount.from_wire(30))),
    ({'kind': 'stale'}, StaleCompaction()),
    ({'kind': 'failed', 'message': 'failure'}, FailedCompaction('failure')),
])
def test_compaction_reply_decodes_directly_to_existing_outcome(result, expected):
    decoded = decode({'type': 'response', 'command': 'compact_durable', 'success': True,
                      'id': '7', 'data': {'outcome': result}})
    assert decoded.command is CompactDurable
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
def test_event_fields_and_changes_are_decoded_at_frame_ingress(event, expected):
    decoded = decode(event)
    assert decoded.convert() == expected
    assert not hasattr(decoded, 'payload')


def test_missing_delta_and_unknown_result_fields_fail_at_ingress():
    with pytest.raises((TypeError, ValueError)):
        decode({'type': 'message_update', 'usage': {}, 'assistantMessageEvent':
            {'type': 'text_delta', 'contentIndex': 0}})
    with pytest.raises(ValueError):
        decode({'type': 'response', 'command': 'compact_durable', 'success': True,
                'id': '1', 'data': {'outcome': {'kind': 'stale', 'message': 'extra'}}})
    assert decode({'type': 'message_update', 'usage': {}}).convert() == ()
    with pytest.raises((TypeError, ValueError)):
        PiEvent.from_wire({'type': 'auto_retry_end', 'attempt': 1})
    assert not isinstance(decode({'type': 'auto_retry_end', 'attempt': 1}), UnknownPiEvent)


def test_result_kind_is_bound_to_its_pending_command():
    async def journey():
        channel = PiRpcChannel(asyncio.StreamReader(), event_type=DurablePiEvent)
        command = CompactDurable(id='9')
        future = channel.track(command)
        with pytest.raises((TypeError, ValueError)):
            decode({'type': 'response', 'command': 'compact_durable', 'id': '9',
                    'success': True, 'data': {'tokens': 0, 'window': None}})
        assert not future.done()
        error = decode({'type': 'response', 'command': 'compact_durable', 'id': '9',
                        'success': False, 'error': 'failed'})
        assert channel.correlate(error) == command
        assert (await future).error == 'failed'
        with pytest.raises(MalformedRecord):
            channel.correlate(error)
    asyncio.run(journey())


def test_real_host_context_and_request_error_remain_typed_and_live(tmp_path, thread_registration, durable_home):
    async def journey():
        backend = PiDurableBackend()
        location = DurableDatabase(str(tmp_path / 'records.sqlite3'))
        thread = thread_registration.new_thread('records', frozenset(), str(tmp_path), backend=PiDurableBackend,
                        session_file=location.path)
        try:
            outcome = await backend.compact(thread, 'retain the empty plan')
            assert isinstance(outcome, FailedCompaction)
            assert 'model' in outcome.message.lower()
            await backend.close_idle()
            first, second = await asyncio.gather(backend.open(thread), backend.open(thread))
            assert first is second
            assert (await (await backend.open(thread)).request(Inspect())).work == ()
            with pytest.raises(RuntimeError):
                await backend.child.request(Settle('task:missing', 'ABANDON'))
            assert (await backend.child.request(Inspect())).work == ()
            assert (await backend.child.request(GetSessionStats())).context_usage == PiContextUsage(PiCount.from_wire(0), UnreportedCount())
            assert (await backend.child.request(Inspect())).work == ()
            assert backend.child.proc.alive()
        finally:
            await backend.close_idle()
    asyncio.run(journey())
