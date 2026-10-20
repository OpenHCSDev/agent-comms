"""Pi events project the tool owner's current state, never infer it from text."""

from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.selected_session import SelectedSession
import asyncio
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from agent_comms.activity import Activity, ActivityState
from agent_comms.agent_events import NativePhaseChanged
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.native_startup import NativeStartup
from agent_comms.native_attestation import LostAttestation
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.pi_events import MessageUpdate, ToolExecutionEnd, ToolExecutionStart, UnknownPiEvent
from agent_comms.pi_payloads import TextDelta
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase
from agent_comms.child_process import ProcessIdentity
from agent_comms.pi_native_backend import PersistentPiSession


@pytest.mark.parametrize("first,second", [
    ("read", "bash"),
    ("x" * (Activity.detail_limit + 1), "y" * (Activity.detail_limit + 1)),
])
async def test_native_tool_events_project_owner_state(first, second):
    root = Path.cwd()
    session = NativeSessionPreparation(NativePiRpcLaunch(("unused",), root, {}, SelectedSession(root), root, root=root, runtime=LaunchRuntime.running(), configuration=RestartEnvironment.inherit({})),
                          native_session=PersistentPiSession(), startup=NativeStartup(root))
    session.native = SimpleNamespace(attestation=LostAttestation(),
                                    proc=SimpleNamespace(identity=ProcessIdentity.capture(os.getpid())))
    session.watchdog.clock = asyncio.get_running_loop().time
    session.watchdog.phase = ModelWaitPhase()

    async def consume(event):
        updates = [update async for update in event.consume(session)]
        phases = [update.phase for update in updates if isinstance(update, NativePhaseChanged)]
        assert phases == [session.watchdog.phase]
        assert phases[0] is session.watchdog.phase
        activity = phases[0].project_activity(Activity("worker", ActivityState.THINKING))
        assert len(activity.detail) <= Activity.detail_limit
        return phases[0]

    updates = [
        update async for update in MessageUpdate(
            assistant_message_event=TextDelta("Starting work")
        ).consume(session)
    ]
    assert not any(isinstance(update, NativePhaseChanged) for update in updates)
    assert isinstance(session.watchdog.phase, ModelWaitPhase)
    phase = await consume(ToolExecutionStart(tool_call_id="one", tool_name=first))
    assert isinstance(phase, ToolRunningPhase)
    assert phase.tools == (("one", first),)
    phase = await consume(ToolExecutionStart(tool_call_id="two", tool_name=second))
    assert phase.tools == (("one", first), ("two", second))
    phase = await consume(ToolExecutionEnd(tool_call_id="one", tool_name=first))
    assert phase.tools == (("two", second),)
    phase = await consume(ToolExecutionEnd(tool_call_id="two", tool_name=second))
    assert isinstance(phase, ModelWaitPhase)
    updates = [update async for update in UnknownPiEvent(payload={}).consume(session)]
    assert not any(isinstance(update, NativePhaseChanged) for update in updates)
