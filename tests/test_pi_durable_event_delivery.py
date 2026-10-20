"""Turn-owned delivery over the real durable host; no provider requests."""
import asyncio
from types import SimpleNamespace

import pytest
from agent_comms.pi_vocabulary import NoThinkingOverride

from agent_comms.agent_events import Chunk, Thinking, ToolStart, Error
from agent_comms.pi_payloads import PiUsage, TextDelta, ThinkingDelta
from agent_comms.pi_events import MessageUpdate, DurablePiEvent, IgnoredDurableEvent, ToolExecutionStart
from agent_comms.pi_commands import GetState, SetThinkingLevel
from agent_comms.pi_vocabulary import LowThinkingLevel, OffThinkingLevel
from agent_comms.pi_durable_backend import (
    DurableDatabase, PiDurableBackend, TaskFailed,
    Inspect, Watch, Unwatch,
)


async def open_host(tmp_path):
    backend = PiDurableBackend()
    thread = SimpleNamespace(worktree=str(tmp_path), model=None, thinking_level=NoThinkingOverride,
                             runtime=DurableDatabase(str(tmp_path / 'durable.sqlite')))
    return await backend.open(thread)


def test_real_host_events_belong_to_open_turn(tmp_path):
    async def journey():
        host = await open_host(tmp_path)
        try:
            async with host.reader.turn() as turn:
                await host.request(Watch())
                assert type(await turn.next_event()) is IgnoredDurableEvent
                await host.request(SetThinkingLevel(level='low'))
                changed = await asyncio.wait_for(turn.next_event(), 5)
                assert type(changed) is IgnoredDurableEvent
                assert (await host.request(GetState())).thinking_level is LowThinkingLevel
                for event in (
                    MessageUpdate(usage=PiUsage(), assistant_message_event=TextDelta('visible')),
                    MessageUpdate(usage=PiUsage(), assistant_message_event=ThinkingDelta('reasoning')),
                    ToolExecutionStart(tool_call_id='call', tool_name='read', args={}),
                    TaskFailed(task_id=1, message='visible failure'),
                ):
                    await turn.deliver_event(event)
                stream = turn.converted()
                try:
                    assert await anext(stream) == Chunk('visible')
                    assert await anext(stream) == Thinking('reasoning')
                    assert isinstance(await anext(stream), ToolStart)
                    assert await anext(stream) == Error('visible failure')
                finally:
                    await stream.aclose()
                await host.request(Unwatch())
            async with host.reader.turn() as next_turn:
                await host.request(Watch())
                assert type(await next_turn.next_event()) is IgnoredDurableEvent
                await host.request(SetThinkingLevel(level='off'))
                changed = await asyncio.wait_for(next_turn.next_event(), 5)
                assert type(changed) is IgnoredDurableEvent
                assert (await host.request(GetState())).thinking_level is OffThinkingLevel
                await host.request(Unwatch())
        finally:
            await host.shutdown()
    asyncio.run(journey())


def test_real_host_event_without_turn_fails_reader(tmp_path):
    async def journey():
        host = await open_host(tmp_path)
        try:
            with pytest.raises(RuntimeError, match='no open turn'):
                await host.request(Watch())
            with pytest.raises(RuntimeError, match='retired'):
                await host.request(Inspect())
        finally:
            await host.shutdown()
    asyncio.run(journey())
