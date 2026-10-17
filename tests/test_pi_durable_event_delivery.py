"""Turn-owned delivery over the real durable host; no provider requests."""
import asyncio
from types import SimpleNamespace

import pytest

from agent_comms.agent_events import Chunk, Thinking, ToolStart, Error
from agent_comms.pi_payloads import PiUsage, TextDelta, ThinkingDelta
from agent_comms.pi_events import BatchMessageUpdate, ToolExecutionStart
from agent_comms.pi_durable_backend import (
    AgentChanged, DurableDatabase, PiDurableBackend, Snapshot, TaskFailed,
    Configure, AgentSettings, Inspect, Watch, Unwatch,
)


async def open_host(tmp_path):
    backend = PiDurableBackend()
    thread = SimpleNamespace(worktree=str(tmp_path), model=None, thinking_level=None,
                             runtime=DurableDatabase(str(tmp_path / 'durable.sqlite')))
    return await backend.open(thread)


def test_real_host_events_belong_to_open_turn(tmp_path):
    async def journey():
        host = await open_host(tmp_path)
        try:
            async with host.turn() as turn:
                await host.request(Watch())
                assert isinstance(await turn.next_event(), Snapshot)
                await host.request(Configure(AgentSettings(instructions='turn-owned event')))
                changed = await asyncio.wait_for(turn.next_event(), 5)
                assert isinstance(changed, AgentChanged)
                assert changed.agent.instructions == 'turn-owned event'
                for event in (
                    BatchMessageUpdate(usage=PiUsage(), changes=(TextDelta('visible'), ThinkingDelta('reasoning'))),
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
            async with host.turn() as next_turn:
                await host.request(Watch())
                assert isinstance(await next_turn.next_event(), Snapshot)
                await host.request(Configure(AgentSettings(instructions='next turn')))
                changed = await asyncio.wait_for(next_turn.next_event(), 5)
                assert changed.agent.instructions == 'next turn'
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
