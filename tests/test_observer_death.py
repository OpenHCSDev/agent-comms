"""The UI side of the comms observer reports the observer's end once, with how it exited, and no later use of it raises a pipe error."""


import asyncio
import os
import signal
from functools import partial

import pytest

from agent_comms.coordination.errors import ReloadRequired
from agent_comms.errors import RelationViolationError
from agent_comms.execution.input_key import BusInputKey
from agent_comms.input_attempt import UnobservedInputDeliveryScope
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.active_route import LocalRoute
from agent_comms.messaging.bus_read import (
    BusRead,
    OwnerScopedRead,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_reads import LiveNotificationRead
from agent_comms.nominal.field_codec import MessageId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.transport.serving.served_requests import OwnerLiveRead
from agent_comms.transport.owner_connection import OwnerIdentityChangedError
from agent_comms.transport.tools.reads import InputDeliveryRead
from agent_comms.ui_model.observation import (
    ObservationProcess,
    ObserverDied,
    ReadNotifications,
    ReadOwnerOperation,
    StopSidebar,
)
from owner_socket_fixture import declare_owner


def owner_reads(family: type[OwnerScopedRead] = OwnerScopedRead):
    """The declared read family supplies the UI operations covered by each contract."""
    return tuple(
        pytest.param(partial(ReadOwnerOperation, operation=operation()), id=operation.declared_name)
        for operation in BusRead.members_with(family)
    )


def empty_window(root) -> ReadNotifications:
    return ReadNotifications(str(root), LiveNotificationRead(()))


async def test_a_killed_observer_is_reported_once_with_its_signal_and_fails_awaited_reads(comms, durable_home):
    async with ObservationProcess.awaited(comms.route) as process:
        async with asyncio.timeout(30):
            assert (await process.read(empty_window(comms.root))).outcomes == {}  # the service runs
        child = process.peer.process.pid
        os.kill(child, signal.SIGSTOP)  # the read below stays unanswered until the child dies
        waiting = asyncio.ensure_future(process.read(empty_window(comms.root)))
        try:
            async with asyncio.timeout(30):
                _, stopped = await asyncio.to_thread(os.waitpid, child, os.WUNTRACED)
            assert os.WIFSTOPPED(stopped)
            await asyncio.sleep(0)
            assert len(process.reads) == 1 and not waiting.done()
            os.kill(child, signal.SIGKILL)
            with pytest.raises(ObserverDied) as died:
                async with asyncio.timeout(30):
                    await waiting
        finally:
            if process.peer.process.exitcode is None:
                os.kill(child, signal.SIGKILL)
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        assert died.value.process.pid == child and died.value.process.exitcode == -signal.SIGKILL
        with pytest.raises(ObserverDied) as stopping:
            process.request(StopSidebar())  # what a closing view sends
        assert stopping.value is died.value
        with pytest.raises(ObserverDied) as later:
            await process.read(empty_window(comms.root))
        assert later.value is died.value


async def test_an_invalid_notification_reference_does_not_kill_the_observer(comms, durable_home):
    async with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(RelationViolationError):
            await process.read(ReadNotifications(str(comms.root), LiveNotificationRead(
                (MessageReference(1, MessageId.mint("not-the-original")),)
            )))
        assert (await process.read(empty_window(comms.root))).outcomes == {}


@pytest.mark.parametrize("read_type", owner_reads(OwnerLiveRead))
async def test_unavailable_live_owner_reads_preserve_the_observer(comms, durable_home, read_type):
    owner = NativeThread("read-subject", frozenset(), Checkout(comms.root.parent))
    comms.registry.declare(owner)
    async with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(RelationViolationError):
            await process.read(read_type(str(comms.root), owner.incarnation))
        assert (await process.read(empty_window(comms.root))).outcomes == {}
        assert process.peer.process.is_alive()
        process.status.raise_if_failed()


@pytest.mark.parametrize("read_type", owner_reads())
async def test_an_unregistered_owner_cannot_supply_a_held_read_incarnation(comms, durable_home, read_type):
    owner = NativeThread("read-subject", frozenset(), Checkout(comms.root.parent))
    async with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(OwnerIdentityChangedError):
            await process.read(read_type(str(comms.root), owner.incarnation))
        assert (await process.read(empty_window(comms.root))).outcomes == {}
        assert process.peer.process.is_alive()
        process.status.raise_if_failed()


@pytest.mark.parametrize("execution", [NativeThread, ExternalThread])
async def test_retained_delivery_reads_preserve_the_observer_without_a_native_queue(
    comms, durable_home, execution,
):
    owner = execution("read-subject", frozenset(), Checkout(comms.root.parent))
    comms.registry.declare(owner)
    ledger = InputDispositions(comms.root)
    key = BusInputKey(1)
    ledger.record(key, seq=1, owner=owner.name, claim_scope=1, target=owner.name, text="Unconfirmed")
    original = ledger.path.read_bytes()
    async with ObservationProcess.awaited(comms.route) as process:
        result = await process.read(ReadOwnerOperation(
            str(comms.root), owner.incarnation, InputDeliveryRead(),
        ))
        assert result.current_scope is UnobservedInputDeliveryScope
        assert [row.input_id for row in result.inputs] == [key]
        assert ledger.path.read_bytes() == original
        assert (await process.read(empty_window(comms.root))).outcomes == {}
        assert process.peer.process.is_alive()
        process.status.raise_if_failed()


@pytest.mark.parametrize("read_type", owner_reads())
@pytest.mark.parametrize("other_root", [False, True])
async def test_stale_owner_reads_preserve_the_observer(comms, durable_home, read_type, other_root):
    owner = NativeThread("read-subject", frozenset(), Checkout(comms.root.parent))
    comms.registry.declare(owner)
    root = str(comms.root.parent / "another-wire") if other_root else str(comms.root)
    identity = owner.incarnation if other_root else RecordedThreadIncarnation(owner.name, 1.0)
    async with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(ReloadRequired if other_root else OwnerIdentityChangedError):
            await process.read(read_type(root, identity))
        assert (await process.read(empty_window(comms.root))).outcomes == {}
        assert process.peer.process.is_alive()
        process.status.raise_if_failed()


@pytest.mark.parametrize("read_type", owner_reads(OwnerLiveRead))
async def test_external_live_owner_reads_refuse_before_waiting_for_a_socket(
    comms, durable_home, runtime_processes, read_type,
):
    """An external bridge has a live process and no runtime socket: the read refuses with the
    original error instead of waiting for a socket that will never be served."""
    owner = declare_owner(comms, "external-owner", runtime_processes[0].identity, ExternalThread)
    async with ObservationProcess.awaited(comms.route) as process:
        with pytest.raises(RelationViolationError):
            async with asyncio.timeout(30):
                await process.read(read_type(str(comms.root), owner.incarnation))
        assert (await process.read(empty_window(comms.root))).outcomes == {}
        assert process.peer.process.is_alive()
        process.status.raise_if_failed()


async def test_an_observer_that_failed_is_reported_with_its_own_traceback_and_exit_status(tmp_path, durable_home):
    not_a_wire = tmp_path / "not-a-wire"
    not_a_wire.write_text("")
    async with ObservationProcess.awaited(LocalRoute(not_a_wire)) as process:
        with pytest.raises(ObserverDied) as died:
            async with asyncio.timeout(30):
                await process.read(empty_window(not_a_wire))
    assert died.value.process.exitcode == 1
    assert "Traceback" in died.value.error and "serve" in died.value.error
    assert died.value.error in str(died.value)
