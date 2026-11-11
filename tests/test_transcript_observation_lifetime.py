"""Observation follows real transcript attachments, never a headless bind."""

import asyncio
import logging
import multiprocessing
from functools import partial

import pytest

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.controller import NO_CONTROLLER
from agent_comms.execution.native_arguments import GivenBinary
from agent_comms.messaging.notification_reads import LiveNotificationRead
from agent_comms.ui_model.observation import (
    LocalRouteObservation,
    ObservationProcess,
    ObserverDied,
    ReadNotifications,
    StopObserving,
)
from delivery_owner_fixture import canonical_agent, start_headless_owner
from test_runtime import until


@pytest.fixture
async def headless_owner(comms, tmp_path, caplog, native_runtime_package):
    owner = canonical_agent(comms, agent_bin=GivenBinary('pi'),
                            auto_wake=False)
    try:
        thread = await Coordination.run_worker(
            partial(owner.sessions.declare_thread, str(tmp_path))
        )
        # canonical_agent connects a FakeClient; a headless owner has no client at all.
        owner.on_connect(NO_CONTROLLER)
        await start_headless_owner(owner, thread.name, owner.inputs.configuration.launch)
        yield owner
    finally:
        await owner.shutdown()
    assert not [record for phase in ('setup', 'call', 'teardown')
                for record in caplog.get_records(phase) if record.levelno >= logging.ERROR]


def observation_children():
    return tuple(child for child in multiprocessing.active_children()
                 if child.name == 'agent-comms-observation')


async def test_headless_bind_has_no_observation_process(headless_owner, tmp_path):
    session, = headless_owner.sessions.published
    state = headless_owner.inputs.state(session).owned()
    await headless_owner.sessions.start_owner(state.configuration.launch, state.owner)
    assert headless_owner.sessions.client is NO_CONTROLLER
    assert headless_owner.sessions.require(session) == session
    assert observation_children() == ()


async def test_subscriptions_read_history_without_an_observation_process(headless_owner):
    """The owner reads its own stores: attached clients never cost it an interpreter."""
    owner = headless_owner
    session, = owner.sessions.published
    thread = owner._comms.registry.require(owner.sessions.require(session))
    frontend = canonical_agent(owner._comms, auto_wake=False)
    first = frontend._create_runtime_proxy(thread, session)
    second = frontend._create_runtime_proxy(thread, session)
    try:
        await first.subscribe()
        await second.subscribe()
        await until(lambda: len(owner._runtime.clients.get(session, ())) == 2)
        assert observation_children() == ()
    finally:
        await first.close()
        await second.close()
        await frontend.shutdown()


async def test_broken_pipe_ends_subscription_with_observer_died(headless_owner):
    process = ObservationProcess(LocalRouteObservation(headless_owner._comms.route))
    pending = asyncio.create_task(process.read(
        ReadNotifications(str(headless_owner._comms.root), LiveNotificationRead(()))))
    try:
        await asyncio.sleep(0)
        process.peer.process.terminate()
        process.peer.process.join(timeout=10)
        assert not process.peer.process.is_alive()
        with pytest.raises(ObserverDied) as failed_send:
            process.request(StopObserving())
        with pytest.raises(ObserverDied) as failed_read:
            await pending
        assert failed_send.value is failed_read.value is process.status
        assert process.ended(None) is failed_send.value
        assert failed_send.value.process is process.peer.process
        assert process.reads == {}
    finally:
        await process.close()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
