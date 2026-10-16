import os
import time
from datetime import datetime

import pytest

from agent_comms.activity import (
    Activity,
    ActivityState,
    FailedPreparingDrainReadiness,
    NativeInputDrainDiagnostic,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.drain_failures import drain_failures
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.threads import Thread
from agent_comms.thread_presentation import AttentionThreadPresentationStatus
from agent_comms.turn_phase import (
    CompactionPhase,
    ModelWaitPhase,
    ProviderRetryPhase,
    ToolRunningPhase,
)


def test_running_native_activity_is_derived_from_current_turn_phase(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(
        Thread(
            "worker",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    first = comms.agents.begin_turn("worker", "first", "Long-running work").turn_lease
    before = comms.registry.snapshot()
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path)))
    after = comms.registry.snapshot()
    assert after.owner_generations == before.owner_generations
    assert after.admission_generations == before.admission_generations
    assert after.threads["worker"].turn_lease == first
    assert comms.registry.require("worker").pid == os.getpid()
    comms.agents.activity.emit(
        Activity("worker", ActivityState.WORKING, "Still working", time.time() - 1000)
    )
    view = comms.views.thread_views()[0]
    assert view.thread.executing and view.presentation.status.busy
    current = comms.agents.activity_of("worker")
    assert current.state is ActivityState.THINKING
    assert current.detail == "Long-running work"

    comms.agents.transition_turn(first, ToolRunningPhase(tools=(("call", "bash"),)))
    assert comms.agents.activity_of("worker").presentation("Worker").summary == "Working · bash"
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert comms.agents.activity_of("worker").presentation("Worker").summary == "Thinking"
    comms.agents.observe_native_phase(first, CompactionPhase())
    assert comms.agents.activity_of("worker").detail == "Compacting context"
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert comms.agents.activity_of("worker").presentation("Worker").summary == "Thinking"
    comms.agents.observe_native_phase(first, ProviderRetryPhase())
    assert comms.agents.activity_of("worker").detail == "Provider retry"
    comms.agents.observe_native_phase(first, ModelWaitPhase())
    assert comms.agents.activity_of("worker").presentation("Worker").summary == "Thinking"

    comms.agents.finish_turn(first)
    assert comms.agents.activity_of("worker").presentation("Worker").summary == "Ready"
    assert not comms.views.thread_views()[0].presentation.status.busy
    comms.agents.begin_turn("worker", "second")
    comms.agents.finish_turn(first)
    assert comms.registry.require("worker").active_turn.id == "second"
    comms.owners.stop("worker")
    assert not comms.registry.require("worker").executing


@pytest.mark.asyncio
async def test_repeated_turn_preparation_displays_real_native_drain_failure(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(
        Thread(
            "worker",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    owner = comms.registry.snapshot().owner_identity("worker")
    retried = 0

    async def retry():
        nonlocal retried
        retried += 1

    async with drain_failures(
        comms, "worker", owner, retry
    ):
        raise NativePiUnavailable("native request never opened")

    failure = comms.agents.activity_of("worker").readiness.source_diagnostic()
    assert isinstance(failure, NativeInputDrainDiagnostic)
    comms.agents.activity.emit(Activity(
        "worker",
        ActivityState.IDLE,
        timestamp=failure.recorded_at + 3600,
        diagnostic=failure,
    ))
    failed_generation = comms.registry.require("worker").turn_generation
    repeated = comms.agents.begin_turn("worker", "repeated-failure", "Preparing queued input")
    assert retried == 1
    assert repeated.turn_lease.identity.generation == failed_generation + 1

    view = comms.views.thread_views()[0]
    assert isinstance(view.activity.readiness, FailedPreparingDrainReadiness)
    recorded = datetime.fromtimestamp(failure.recorded_at).astimezone().strftime("%H:%M")
    assert view.presentation.summary == f"Failed {recorded}"
    assert "native request never opened" in failure.summary
    assert isinstance(view.presentation.status, AttentionThreadPresentationStatus)
    assert view.presentation.status.busy
    comms.agents.finish_turn(repeated.turn_lease)
