"""Typed durable JSONL ingress and real-host replies, without provider work."""
import asyncio
from pathlib import Path

import pytest

from agent_comms.agent_backend import (
    ContextUsage, FailedCompaction, NothingToCompaction, PlacedCompaction, StaleCompaction,
)
from agent_comms.agent_events import Chunk, Thinking, ToolStart, Error
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_durable_backend import (
    CompactHostCommand, CompactionHostMessage, ContextUsageHostCommand,
    DurableDatabase, DurableHost, HostMessage, InspectHostCommand, PendingHostRequest,
    PiDurableBackend, SettleHostCommand,
)
from agent_comms.threads import Thread


@pytest.mark.parametrize('result,expected', [
    ({'kind': 'nothing_to'}, NothingToCompaction()),
    ({'kind': 'placed', 'summary': 'summary', 'first_kept': '7', 'tokens_before': 30},
     PlacedCompaction('summary', '7', 30)),
    ({'kind': 'stale'}, StaleCompaction()),
    ({'kind': 'failed', 'message': 'failure'}, FailedCompaction('failure')),
])
def test_compaction_reply_decodes_directly_to_existing_outcome(result, expected):
    wire = {'kind': 'compaction', 'id': 7, 'result': result}
    decoded = FieldCodec.decode(HostMessage, wire)
    assert isinstance(decoded, CompactionHostMessage)
    assert decoded.result == expected
    assert FieldCodec.encode(decoded) == wire


@pytest.mark.parametrize('event,expected', [
    ({'type': 'message_update', 'usage': {}, 'changes': [
        {'type': 'text_delta', 'contentIndex': 0, 'delta': 'visible'},
        {'type': 'thinking_delta', 'contentIndex': 1, 'delta': 'reasoning'},
    ]}, (Chunk('visible'), Thinking('reasoning'))),
    ({'type': 'tool_execution_start', 'toolCallId': 'call', 'toolName': 'read',
      'args': {'path': 'source.py'}},
     (ToolStart(id='call', name='read', args={'path': 'source.py'}),)),
    ({'type': 'task_failed', 'taskId': 3, 'kind': 'tool', 'message': 'failed'},
     (Error('failed'),)),
])
def test_event_fields_and_changes_are_decoded_at_frame_ingress(event, expected):
    decoded = FieldCodec.decode(HostMessage, {'kind': 'event', 'event': event})
    assert decoded.event.convert() == expected
    assert not hasattr(decoded.event, 'payload')


def test_missing_delta_and_unknown_result_fields_fail_at_ingress():
    with pytest.raises((TypeError, ValueError), match='delta'):
        FieldCodec.decode(HostMessage, {'kind': 'event', 'event': {
            'type': 'message_update', 'usage': {}, 'changes': [
                {'type': 'text_delta', 'contentIndex': 0},
            ],
        }})
    with pytest.raises(ValueError, match='Unknown fields'):
        FieldCodec.decode(HostMessage, {'kind': 'compaction', 'id': 1,
                                       'result': {'kind': 'stale', 'message': 'extra'}})


def test_result_kind_is_bound_to_its_pending_command():
    async def journey():
        host = DurableHost()
        future = asyncio.get_running_loop().create_future()
        host.pending[9] = PendingHostRequest(CompactHostCommand(None), future)
        wrong = FieldCodec.decode(HostMessage, {
            'kind': 'context', 'id': 9, 'result': {'tokens': 0, 'window': None},
        })
        with pytest.raises(ValueError, match='Wrong reply to compact'):
            wrong.deliver(host)
        # The reader still owns the failed request, so its failure handler can
        # finish this future too; it must not vanish from the pending requests.
        assert host.pending[9].future is future
        error = FieldCodec.decode(HostMessage, {'kind': 'error', 'id': 9, 'error': 'failed'})
        error.deliver(host)
        with pytest.raises(RuntimeError, match='failed'):
            await future
        assert not host.pending
    asyncio.run(journey())


def test_real_host_context_and_request_error_remain_typed_and_live(tmp_path):
    async def journey():
        backend = PiDurableBackend()
        location = DurableDatabase(str(tmp_path / 'records.sqlite3'))
        thread = Thread('records', frozenset(), str(tmp_path), backend=PiDurableBackend,
                        session_file=location.path)
        try:
            assert await backend.context_usage(thread) == ContextUsage(0, None)
            assert await backend.interrupted(thread) == ()
            with pytest.raises(RuntimeError, match='Interrupted work does not exist'):
                await backend.host.request(SettleHostCommand('task:missing', 'ABANDON'))
            assert await backend.host.request(InspectHostCommand()) == ()
            assert await backend.host.request(ContextUsageHostCommand()) == ContextUsage(0, None)
            assert not backend.host.reader.done()
        finally:
            await backend.close_idle()
    asyncio.run(journey())
