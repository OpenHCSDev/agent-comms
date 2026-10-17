"""Turn-owned delivery over the real durable host; no provider requests."""
import asyncio
from types import SimpleNamespace

import pytest

from agent_comms.agent_events import Chunk, Thinking, ToolStart, Error
from agent_comms.pi_durable_backend import (
    AgentChangedDurableEvent, DurableHost, MessageUpdateDurableEvent,
    SnapshotDurableEvent, TaskFailedDurableEvent, ToolExecutionStartDurableEvent,
)


async def open_host(tmp_path):
    host = DurableHost()
    thread = SimpleNamespace(worktree=str(tmp_path), model=None, thinking_level=None)
    await host.open(SimpleNamespace(path=str(tmp_path / 'durable.sqlite')), thread)
    return host


def test_real_host_events_belong_to_open_turn(tmp_path):
    async def journey():
        host = await open_host(tmp_path)
        try:
            async with host.open_turn() as turn:
                assert isinstance(await turn.next_event(), SnapshotDurableEvent)
                await host.request('configure', agent={'instructions': 'turn-owned event'})
                changed = await asyncio.wait_for(turn.next_event(), 5)
                assert isinstance(changed, AgentChangedDurableEvent)
                assert changed.payload['agent']['instructions'] == 'turn-owned event'
                # Same turn delivers Core-visible records, not a host-side sink.
                for event in (
                    MessageUpdateDurableEvent({'changes': [
                        {'type': 'text_delta', 'delta': 'visible'},
                        {'type': 'thinking_delta', 'delta': 'reasoning'},
                    ]}),
                    ToolExecutionStartDurableEvent({'toolCallId': 'call', 'toolName': 'read', 'args': {}}),
                    TaskFailedDurableEvent({'message': 'visible failure'}),
                ):
                    turn.receive(event)
                stream = turn.converted()
                try:
                    assert await anext(stream) == Chunk('visible')
                    assert await anext(stream) == Thinking('reasoning')
                    assert isinstance(await anext(stream), ToolStart)
                    assert await anext(stream) == Error('visible failure')
                finally:
                    await stream.aclose()
            # Watch retirement is drained before ownership is released. Another
            # turn receives its own snapshot and subsequent real host update.
            async with host.open_turn() as next_turn:
                assert isinstance(await next_turn.next_event(), SnapshotDurableEvent)
                await host.request('configure', agent={'instructions': 'next turn'})
                changed = await asyncio.wait_for(next_turn.next_event(), 5)
                assert changed.payload['agent']['instructions'] == 'next turn'
        finally:
            await host.close()
    asyncio.run(journey())


def test_real_host_event_without_turn_fails_reader(tmp_path):
    async def journey():
        host = await open_host(tmp_path)
        try:
            # Deliberately violate ownership through the actual JSONL protocol.
            with pytest.raises(RuntimeError, match='no open turn'):
                await host.request('watch')
                await asyncio.wait_for(asyncio.shield(host.reader), 5)
            with pytest.raises(RuntimeError, match='no open turn'):
                await host.request('inspect')
        finally:
            await host.close()
    asyncio.run(journey())
