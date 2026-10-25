from agent_comms.repo import Checkout
import os
import time
from agent_comms.activity import (
    Activity,
    ActivityState,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.field_codec import TurnId
from agent_comms.native_tool_call import ToolCallId
from agent_comms.threads import Thread
from agent_comms.turn_phase import (
    CompactionPhase,
    ModelWaitPhase,
    ProviderRetryPhase,
    ToolRunningPhase,
)


def test_running_native_activity_is_derived_from_current_turn_phase(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(
        Thread('worker', frozenset(), Checkout.from_wire(str(tmp_path)), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    first = comms.agents.begin_turn(
        "worker", TurnId('a7937b64b8caa58f03721bb6bacf5c78'), "Long-running work"
    ).turn_lease
    before = comms.registry.snapshot()
    comms.registry.declare(Thread('worker', frozenset(), Checkout.from_wire(str(tmp_path))))
    after = comms.registry.snapshot()
    assert after.owner_generations == before.owner_generations
    assert after.claim_generation('worker') == before.claim_generation('worker')
    assert after.threads["worker"].turn_lease == first
    assert comms.registry.require("worker").require_process().pid == os.getpid()
    comms.agents.activity.emit(
        Activity("worker", ActivityState.WORKING, "Still working", time.time() - 1000)
    )
    view = comms.views.thread_views()[0]
    assert view.thread.executing and view.presentation.status.working
    current = comms.agents.activity_of("worker", snapshot=comms.registry.snapshot())
    assert current.state is ActivityState.STARTING
    assert current.detail == "Long-running work"
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).state is ActivityState.THINKING

    comms.agents.transition_turn(first, ToolRunningPhase(tools=((ToolCallId("call"), "bash"),)))
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).presentation("Worker").summary == "Working · bash"
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).presentation("Worker").summary == "Thinking"
    comms.agents.observe_native_phase(first, CompactionPhase())
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).detail == "Compacting context"
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).presentation("Worker").summary == "Thinking"
    comms.agents.observe_native_phase(first, ProviderRetryPhase())
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).detail == "Provider retry"
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).presentation("Worker").summary == "Thinking"

    comms.agents.finish_turn(first)
    assert comms.agents.activity_of("worker", snapshot=comms.registry.snapshot()).presentation("Worker").summary == "Ready"
    assert not comms.views.thread_views()[0].presentation.status.working
    comms.agents.begin_turn("worker", TurnId('16367aacb67a4a017c8da8ab95682ccb'))
    comms.agents.finish_turn(first)
    assert (
        comms.registry.require("worker").turn_record.read().require_active().id
        == '16367aacb67a4a017c8da8ab95682ccb'
    )
    comms.owners.stop("worker")
    assert not comms.registry.require('worker').executing
