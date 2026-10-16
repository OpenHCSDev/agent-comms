"""The UI side of the comms observer reports the observer's end once, with how it exited, and no later use of it raises a pipe error."""

import asyncio
import os
import signal

import pytest

from agent_comms.active_route import LocalRoute
from agent_comms.errors import RelationViolationError
from agent_comms.message_reference import MessageReference
from agent_comms.ui_model.observation import ObservationProcess, ObserverDied, ReadNotifications, StopSidebar


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


async def test_a_killed_observer_is_reported_once_with_its_signal_and_fails_awaited_reads(comms):
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


async def test_an_invalid_notification_reference_does_not_kill_the_observer(comms):
    comms.messaging.initialize_private_initial_protocol()
    with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(RelationViolationError, match="not its original source"):
            await process.read(ReadNotifications(
                str(comms.root), (MessageReference(1, "not-the-original"),)
            ))
        assert await process.read(ReadNotifications(str(comms.root), ())) == {}


async def test_an_observer_that_failed_is_reported_with_its_own_traceback_and_exit_status(tmp_path):
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
