"""Actual Pi event effects publish their observer phase, never infer it from text."""

import asyncio
from pathlib import Path

from agent_comms.agent_events import NativePhaseChanged
from agent_comms.backend import TurnSession
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart, UnknownPiEvent
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase


async def test_native_tool_lifecycle_publishes_actual_phase_after_effects():
    root = Path.cwd()
    session = TurnSession(NativePiRpcLaunch(("unused",), root, {}, root, None, root), "unused")
    session.active_tools = set()
    session.watchdog.clock = asyncio.get_running_loop().time
    session.watchdog.phase = ModelWaitPhase()

    async def consume(event):
        updates = [update async for update in event.consume(session)]
        phases = [update.phase for update in updates if isinstance(update, NativePhaseChanged)]
        assert phases == [session.watchdog.phase]
        assert phases[0] is session.watchdog.phase
        return phases[0]

    phase = await consume(ToolExecutionStart(tool_call_id="one", tool_name="read"))
    assert isinstance(phase, ToolRunningPhase)
    assert phase.tools == (("one", "read"),)
    phase = await consume(ToolExecutionStart(tool_call_id="two", tool_name="bash"))
    assert phase.tools == (("one", "read"), ("two", "bash"))
    phase = await consume(ToolExecutionEnd(tool_call_id="one", tool_name="read"))
    assert phase.tools == (("two", "bash"),)
    phase = await consume(ToolExecutionEnd(tool_call_id="two", tool_name="bash"))
    assert isinstance(phase, ModelWaitPhase)
    updates = [update async for update in UnknownPiEvent(payload={}).consume(session)]
    assert not any(isinstance(update, NativePhaseChanged) for update in updates)
