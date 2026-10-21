"""Observation follows real transcript attachments, never a headless bind."""

import asyncio
import logging
import multiprocessing
import os
from functools import partial

import pytest

from agent_comms.coordinator import Coordination
from agent_comms.native_arguments import GivenBinary
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.ui_model.observation import (
    ObservationProcess,
    ObserverDied,
    ReadNotifications,
    StopObserving,
)
from delivery_owner_fixture import canonical_agent
from test_runtime import until


@pytest.fixture
async def headless_owner(comms, tmp_path, caplog):
    owner = canonical_agent(comms, agent_bin=GivenBinary('pi'),
                            runtime_enabled=True, auto_wake=False)
    owner.on_connect(None)
    try:
        thread = await Coordination.run_worker(
            partial(owner.sessions.declare_thread, str(tmp_path))
        )
        await owner.sessions.start_owner(str(tmp_path), thread.name)
        yield owner
    finally:
        await owner.shutdown()
    assert not [record for phase in ('setup', 'call', 'teardown')
                for record in caplog.get_records(phase) if record.levelno >= logging.ERROR]


def observation_children():
    return tuple(child for child in multiprocessing.active_children()
                 if child.name == 'agent-comms-observation')


async def test_headless_bind_has_no_observation_process(headless_owner, tmp_path):
    session, = headless_owner.sessions.bindings
    await headless_owner.sessions.start_owner(str(tmp_path), session)
    assert headless_owner.sessions.client is None
    assert headless_owner.sessions.require(session) == session
    assert observation_children() == ()


async def test_subscriptions_share_process_and_last_close_joins_it(headless_owner):
    owner = headless_owner
    session, = owner.sessions.bindings
    frontend = canonical_agent(owner._comms, auto_wake=False)
    first = RuntimeProxy(frontend, session, socket_path(owner._comms.root, os.getpid()))
    second = RuntimeProxy(frontend, session, socket_path(owner._comms.root, os.getpid()))
    reopened = RuntimeProxy(frontend, session, socket_path(owner._comms.root, os.getpid()))
    try:
        assert observation_children() == ()
        await first.subscribe()
        process, = observation_children()
        await second.subscribe()
        assert observation_children() == (process,)
        await first.close()
        await until(lambda: len(owner._runtime.clients.get(session, ())) == 1)
        assert process.is_alive()
        await second.close()
        await until(lambda: not process.is_alive())
        assert process.exitcode == 0
        assert observation_children() == ()
        await reopened.subscribe()
        replacement, = observation_children()
        assert replacement.pid != process.pid
        await reopened.close()
        await until(lambda: not replacement.is_alive())
        assert replacement.exitcode == 0
    finally:
        await first.close()
        await second.close()
        await reopened.close()
        await frontend.shutdown()


async def test_broken_pipe_ends_subscription_with_observer_died(headless_owner):
    process = ObservationProcess(headless_owner._comms.route)
    pending = asyncio.create_task(process.read(
        ReadNotifications(str(headless_owner._comms.root), ())))
    try:
        await asyncio.sleep(0)
        process.process.terminate()
        process.process.join(timeout=10)
        assert not process.process.is_alive()
        with pytest.raises(ObserverDied) as failed_send:
            process.request(StopObserving())
        with pytest.raises(ObserverDied) as failed_read:
            await pending
        assert failed_send.value is failed_read.value is process.status
        assert process.ended(None) is failed_send.value
        assert failed_send.value.pid == process.process.pid
        assert process.reads == {}
    finally:
        process.close()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
