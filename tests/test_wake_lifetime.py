"""Owner background controller isolation and cancellation before native dispatch."""


import asyncio
import os

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.routing import ScheduledTurn
from agent_comms.controller import UNBOUND_CONTROLLER
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


async def test_wake_lock_wait_shutdown_keeps_input_unsent_and_controller_private(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(
        Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    agent = CommsAgent(comms, auto_wake=True, runtime_enabled=True)
    agent.sessions.bindings["owner"] = "owner"
    inputs = agent.inputs
    controller = object()
    token = inputs.runtime.controller.set(controller)
    lock = agent.turns.turn_locks.setdefault("owner", asyncio.Lock())
    await lock.acquire()
    queued = ScheduledTurn("Never dispatch this cancelled wake")
    inputs.state("owner").pending_turns = [queued]
    try:

        async def observe_controller():
            assert inputs.runtime.controller.get() is UNBOUND_CONTROLLER

        await inputs.runtime.background(observe_controller())
        assert inputs.runtime.controller.get() is controller
        check = WakeScheduleCheck(session_id="owner", inputs=inputs)
        check.schedule()
        task = inputs.state("owner").wake_task
        check.schedule()
        assert inputs.state("owner").wake_task is task
        await asyncio.sleep(0)
        assert not task.done()
        assert inputs.state("owner").pending_turns == [queued]
        await asyncio.wait_for(inputs.close(), 1)
        assert task.cancelled()
        assert inputs.state("owner").pending_turns == [queued]
        assert not stored_inputs(inputs.dispositions.root).rows
        assert not agent.turns.turn_tasks
        check.schedule()
        assert not any(state.wake_task for state in inputs.drained.values())
        assert inputs.runtime.controller.get() is controller
    finally:
        lock.release()
        inputs.runtime.controller.reset(token)
        await agent.shutdown()
