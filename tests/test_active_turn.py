import os
import time

from agent_comms.comms import wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.turn_phase import (
    CompactionPhase,
    IdlePhase,
    ModelWaitPhase,
    ProviderRetryPhase,
    ToolRunningPhase,
)
from agent_comms.nominal.field_codec import (
    ToolCallId,
    TurnId,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import (
    Activity,
    ActivityState,
)
from agent_comms.registry.threads import NativeThread


def test_running_native_activity_is_derived_from_current_turn_phase(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(
        NativeThread('worker', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    first = comms.agents.begin_turn(
        "worker", TurnId('a7937b64b8caa58f03721bb6bacf5c78'), "Long-running work"
    ).turn_lease
    before = comms.registry.snapshot()
    comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path)))
    after = comms.registry.snapshot()
    assert after.owner_generations == before.owner_generations
    assert after.claim_generation('worker') == before.claim_generation('worker')
    assert after.threads["worker"].turn_lease == first
    assert comms.registry.require("worker").require_process().pid == os.getpid()
    comms.agents.activity.emit(
        Activity("worker", ActivityState.WORKING, "Still working", time.time() - 1000)
    )
    view = comms.views.thread_views()[0]
    assert view.thread.executing and view.presentation.status.is_working()
    current = comms.agents.activity_of("worker", snapshot=comms.registry.snapshot())
    assert current.state is ActivityState.STARTING
    assert current.detail == "Long-running work"
    def activity():
        return comms.agents.activity_of("worker", snapshot=comms.registry.snapshot())

    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert activity().state is ActivityState.THINKING
    comms.agents.transition_turn(first, ToolRunningPhase(tools=((ToolCallId("call"), "bash"),)))
    assert "bash" in activity().presentation("Worker").summary
    for excursion in (CompactionPhase(), ProviderRetryPhase()):
        comms.agents.observe_native_phase(first, ModelWaitPhase())
        assert activity().state is ActivityState.THINKING
        comms.agents.observe_native_phase(first, excursion)
        assert activity().detail == excursion.label
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert activity().state is ActivityState.THINKING

    comms.agents.finish_turn(first)
    assert activity().presentation("Worker").summary == IdlePhase.label
    assert not comms.views.thread_views()[0].presentation.status.is_working()
    comms.agents.begin_turn("worker", TurnId('16367aacb67a4a017c8da8ab95682ccb'))
    comms.agents.finish_turn(first)
    assert (
        comms.registry.require("worker").turn_record.read().require_active().id
        == '16367aacb67a4a017c8da8ab95682ccb'
    )
