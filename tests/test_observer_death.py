"""The UI side of the comms observer reports the observer's end once, with how it exited, and no later use of it raises a pipe error."""

from agent_comms.bus import Bus

import asyncio
import os
import signal

import pytest

from agent_comms.active_route import LocalRoute
from agent_comms.coordination_errors import StaleRevision
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.runtime import OwnerIdentityChangedError
from agent_comms.message_reference import MessageReference
from agent_comms.ui_model.observation import (
    ObservationProcess, ObserverDied, OwnerRead, ReadNotifications, StopSidebar,
)


async def readable(process: ObservationProcess) -> None:
    """Wait until the observer's pipe has something to read, as the UI's event loop does."""
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_reader(process.fileno(), ready.set)
    try:
        async with asyncio.timeout(30):
            await ready.wait()
    finally:
        loop.remove_reader(process.fileno())


async def test_a_killed_observer_is_reported_once_with_its_signal_and_fails_awaited_reads(comms, durable_home):
    process = ObservationProcess(comms.route)
    try:
        await readable(process)
        assert process.results()  # the first observation: the service is running
        os.kill(process.process.pid, signal.SIGSTOP)  # the read below is still unanswered when it dies
        waiting = asyncio.ensure_future(process.read(ReadNotifications(str(comms.root), ())))
        await asyncio.sleep(0)
        os.kill(process.process.pid, signal.SIGKILL)
        with pytest.raises(ObserverDied) as died:
            while True:
                await readable(process)
                process.results()
        assert died.value.pid == process.process.pid and died.value.exit_status == -signal.SIGKILL
        assert f"pid {process.process.pid}" in str(died.value) and "signal 9" in str(died.value)
        with pytest.raises(ObserverDied) as awaited:
            await waiting
        assert awaited.value is died.value
        process.request(StopSidebar())  # what a closing view sends; the observer's end was already reported
        with pytest.raises(ObserverDied) as later:
            await process.read(ReadNotifications(str(comms.root), ()))
        assert later.value is died.value
    finally:
        process.close()


async def test_an_invalid_notification_reference_does_not_kill_the_observer(comms, durable_home):
    Bus.install(Bus.path_in(comms.root))
    with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(RelationViolationError):
            await process.read(ReadNotifications(
                str(comms.root), (MessageReference(1, "not-the-original"),)
            ))
        assert await process.read(ReadNotifications(str(comms.root), ())) == {}


@pytest.mark.parametrize("read_type", OwnerRead.__subclasses__())
@pytest.mark.parametrize("declared, refusal", [
    (True, RelationViolationError), (False, UnregisteredThreadError),
])
async def test_unavailable_owner_reads_preserve_the_observer(
    comms, durable_home, read_type, declared, refusal,
):
    owner = comms.registry.new_thread("read-subject", frozenset(), str(comms.root.parent))
    if declared:
        comms.registry.declare(owner)
    with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(refusal):
            await process.read(read_type(str(comms.root), owner.incarnation))
        assert await process.read(ReadNotifications(str(comms.root), ())) == {}
        assert process.process.is_alive()
        process.status.raise_if_failed()


@pytest.mark.parametrize("read_type", OwnerRead.__subclasses__())
@pytest.mark.parametrize("other_root", [False, True])
async def test_stale_owner_reads_preserve_the_observer(comms, durable_home, read_type, other_root):
    owner = comms.registry.new_thread("read-subject", frozenset(), str(comms.root.parent))
    comms.registry.declare(owner)
    root = str(comms.root.parent / "another-wire") if other_root else str(comms.root)
    identity = owner.incarnation if other_root else RecordedThreadIncarnation(owner.name, 1.0)
    with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(StaleRevision if other_root else OwnerIdentityChangedError):
            await process.read(read_type(root, identity))
        assert await process.read(ReadNotifications(str(comms.root), ())) == {}
        assert process.process.is_alive()
        process.status.raise_if_failed()


async def test_an_observer_that_failed_is_reported_with_its_own_traceback_and_exit_status(tmp_path, durable_home):
    not_a_wire = tmp_path / "not-a-wire"
    not_a_wire.write_text("")
    process = ObservationProcess(LocalRoute(not_a_wire))
    try:
        with pytest.raises(ObserverDied) as died:
            while True:
                await readable(process)
                process.results()
        assert died.value.exit_status == 1
        assert "Traceback" in died.value.error and "serve" in died.value.error
        assert "exited with status 1" in str(died.value) and died.value.error in str(died.value)
    finally:
        process.close()
