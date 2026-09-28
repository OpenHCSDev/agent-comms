import os
import time

from agent_comms import wire
from agent_comms.activity import Activity, ActivityState
from agent_comms.threads import Thread


def test_silent_active_turn_does_not_age_into_ready(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("worker", frozenset(), str(tmp_path), pid=os.getpid()))
    first = comms.begin_turn("worker", "first", "Long-running work")
    before = comms.registry.snapshot()
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    after = comms.registry.snapshot()
    assert after.owner_generations == before.owner_generations
    assert after.admission_generations == before.admission_generations
    assert after.threads["worker"].turn_lease == first
    assert comms.registry.require("worker").pid == os.getpid()
    comms.activity.emit(
        Activity("worker", ActivityState.WORKING, "Still working", time.time() - 1000)
    )
    view = comms.thread_views()[0]
    assert view.thread.executing and view.presentation.busy
    assert comms.activity_of("worker").state is ActivityState.WORKING
    comms.finish_turn(first)
    assert not comms.thread_views()[0].presentation.busy
    comms.begin_turn("worker", "second")
    comms.finish_turn(first)
    assert comms.registry.require("worker").active_turn.id == "second"
    comms.stop("worker")
    assert not comms.registry.require("worker").executing
