"""Owner background controller isolation and cancellation before native dispatch."""

import asyncio
import os

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.routing import ScheduledTurn
from agent_comms.runtime import UNBOUND_CONTROLLER
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.threads import Thread


async def test_wake_lock_wait_shutdown_keeps_input_unsent_and_controller_private(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    agent = CommsAgent(comms, auto_wake=True, runtime_enabled=True)
    agent.sessions.bindings["owner"] = "owner"
    inputs = agent.inputs
    controller = object()
    token = inputs.runtime.controller.set(controller)
    lock = agent.turns.turn_locks.setdefault("owner", asyncio.Lock())
    await lock.acquire()
    queued = ScheduledTurn("Never dispatch this cancelled wake")
    inputs.pending_turns["owner"] = [queued]
    try:

        async def observe_controller():
            assert inputs.runtime.controller.get() is UNBOUND_CONTROLLER

        await inputs.runtime.background(observe_controller())
        assert inputs.runtime.controller.get() is controller
        check = WakeScheduleCheck(session_id="owner", inputs=inputs)
        check.schedule()
        task = inputs.wake_tasks["owner"]
        check.schedule()
        assert inputs.wake_tasks["owner"] is task
        await asyncio.sleep(0)
        assert not task.done()
        assert inputs.pending_turns["owner"] == [queued]
        await asyncio.wait_for(inputs.stop_wakes(), 1)
        assert task.cancelled()
        assert inputs.pending_turns["owner"] == [queued]
        assert not inputs.dispositions.read().rows
        assert not agent.turns.turn_tasks
        check.schedule()
        assert not inputs.wake_tasks
        assert inputs.runtime.controller.get() is controller
    finally:
        lock.release()
        inputs.runtime.controller.reset(token)
        await agent.shutdown()
