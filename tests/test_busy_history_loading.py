"""Source contention retains a viewer read, never manufactures an input failure."""

import asyncio
import json
import subprocess
import sys
from contextlib import contextmanager
from multiprocessing import Pipe

import pytest

from agent_comms.acp_extension import TranscriptSnapshotUpdate
from agent_comms.comms import wire
from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.messaging.notification_reads import LiveNotificationRead
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import UnrecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tools.reads import OwnerGoalRead, TranscriptPageRead
from agent_comms.ui_model.observation import (
    NothingAnswered,
    NotificationsObserved,
    ObservationProcess,
    ObserveNotifications,
    ReadBusy,
    ReadNotifications,
    ReadOwnerOperation,
    ReadRefused,
    ReadTranscript,
    RevisionObserved,
)
from agent_comms.ui_model.observation_service import (
    LocalRouteObservation,
    ObservationService,
)


def saved_history(tmp_path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "saved.jsonl"
    timestamp = "2026-10-20T00:00:00.000Z"
    session.write_text("".join(json.dumps(record) + "\n" for record in (
        {"type": "session", "version": 3, "id": "saved-session", "timestamp": timestamp},
        {"type": "message", "id": "saved-answer", "parentId": None, "timestamp": timestamp,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Saved answer"}]}},
    )))
    comms.registry.declare(NativeThread('saved', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(session), comms.root)))
    comms.registry.store.prepare_native_root()
    return comms, session


@contextmanager
def held_source(comms):
    # A separate writer also keeps raw header inspection in the reader from
    # altering a same-process POSIX lock. No provider or user input is involved.
    child = subprocess.Popen([
        sys.executable, "-c",
        "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
        "db.execute('BEGIN EXCLUSIVE'); print('locked',flush=True); "
        "sys.stdin.readline(); db.rollback(); db.close()",
        str(comms.root / "coordination.sqlite3"),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        yield
    finally:
        child.stdin.write("release\n")
        child.stdin.flush()
        child.communicate(timeout=5)
        assert child.returncode == 0


async def test_busy_source_keeps_original_waiter_and_other_reads_move(tmp_path):
    comms, session = saved_history(tmp_path)
    original = session.read_bytes()
    async with LocalRouteObservation.awaited(comms.route) as process:
        with held_source(comms):
            answer = ReadTranscript(str(comms.root), TranscriptPageRead(thread="saved"), request_id=19).answer(comms)
            assert isinstance(answer, ReadBusy)
            assert answer.title == "History loading"
            waiting = asyncio.create_task(process.read(ReadTranscript(str(comms.root), TranscriptPageRead(thread="saved"))))
            independent = asyncio.create_task(process.read(ReadNotifications(str(comms.root), LiveNotificationRead(()))))
            async with asyncio.timeout(15):
                assert (await independent).outcomes == {}
            assert not waiting.done()
            assert len(process.reads) == 1
            (request_id,) = process.reads
            original_waiter = process.reads[request_id]
            await asyncio.sleep(0.15)
            assert process.reads[request_id] is original_waiter
            # The writer leaves by rollback, without changing the revision.
        async with asyncio.timeout(5):
            snapshot = await waiting
        assert isinstance(snapshot, TranscriptSnapshotUpdate)
        assert [event.text for event in snapshot.page.events] == ["Saved answer"]
        assert snapshot.identity.runtime.read_key == str(session)
        assert process.reads == {}
    assert session.read_bytes() == original


async def test_cancelled_busy_read_is_removed_without_resubmission(tmp_path):
    comms, _session = saved_history(tmp_path)
    async with LocalRouteObservation.awaited(comms.route) as process:
        # The observer is a spawned process that imports Comms cold. Let it answer once before
        # the source is held, so the cancelled read reaches a running service and the 5 s below
        # times a read, not the child's start-up (on loaded CI runners it took longer than that).
        await process.read(ReadNotifications(str(comms.root), LiveNotificationRead(())))
        with held_source(comms):
            waiting = asyncio.create_task(process.read(ReadTranscript(str(comms.root), TranscriptPageRead(thread="saved"))))
            await asyncio.sleep(0.15)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert process.reads == {}
        async with asyncio.timeout(5):
            assert (await process.read(ReadNotifications(str(comms.root), LiveNotificationRead(())))).outcomes == {}
        assert process.reads == {}


@pytest.mark.parametrize("error", [GeneratorExit(), asyncio.CancelledError(), RuntimeError("original caller failure")])
async def test_awaited_scope_preserves_exit_and_closes_once(comms, monkeypatch, error):
    from multiprocessing.connection import Connection

    process = LocalRouteObservation.awaited(comms.route)
    original = Connection.close
    closes = []

    def closed(connection):
        if connection is process.connection:
            closes.append(connection)
        return original(connection)

    monkeypatch.setattr(Connection, "close", closed)
    with pytest.raises(type(error)) as raised:
        async with process:
            raise error
    assert raised.value is error
    await process.close()
    assert closes == [process.connection]
    assert process.connection.closed and process.peer.process.exitcode == 0


def test_observer_start_failure_releases_both_real_pipe_endpoints(comms, monkeypatch):
    from multiprocessing.process import BaseProcess

    endpoints = []
    failure = OSError("original spawn failure")

    def failed(process):
        endpoints.extend(process._args[1:])
        raise failure

    # Inspect the actual Pipe endpoints without replacing their implementation.
    from multiprocessing.connection import Connection
    close = Connection.close
    closed = []

    def closing(connection):
        closed.append(connection)
        return close(connection)

    monkeypatch.setattr(Connection, "close", closing)
    monkeypatch.setattr(BaseProcess, "start", failed)
    with pytest.raises(OSError) as raised:
        ObservationProcess(LocalRouteObservation(comms.route))
    assert raised.value is failure
    assert len(closed) == 2 and all(connection.closed for connection in closed)
    assert endpoints and endpoints[0] in closed


def test_viewer_reset_with_unread_observations_ends_the_service(comms):
    import multiprocessing


    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        connection.send(RevisionObserved(comms.views.revision()))
        viewer.close()  # The unread observation makes the real socketpair reset.
        service.receive()
        assert service.closed and service.changed.is_set()


async def test_peer_write_failure_retires_once_and_cancels_owner_reads(comms):
    import multiprocessing


    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        asking = asyncio.create_task(asyncio.Event().wait())
        service.owner_reads[7] = asking
        viewer.close()
        # The real Connection pickler exports a BytesIO memoryview for this payload.
        service.send(ReadRefused(1, RuntimeError("large"), "x" * 20_000))
        assert service.closed and service.changed.is_set()
        assert asking.cancelling() == 1
        service.retire()
        service.send(NothingAnswered(2))
        service.receive()
        assert asking.cancelling() == 1
        with pytest.raises(asyncio.CancelledError):
            await asking


async def test_owner_reply_uses_the_same_peer_retirement(comms):
    import multiprocessing


    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        # Owner reads live in the service's task group, which only its run enters.
        async with service.tasks:
            # A real missing-owner refusal still has to be sent through the shared endpoint.
            request = ReadOwnerOperation(str(comms.root), UnrecordedThreadIncarnation("missing"),
                                         request_id=7, operation=OwnerGoalRead())
            service.accept(request)
            asking = service.owner_reads[7]
            viewer.close()
            async with asyncio.timeout(5):
                await asking
        assert not service.owner_reads
        assert service.closed and service.changed.is_set()


async def test_awaited_notifications_then_immediate_close_retires_successfully(comms):
    async with LocalRouteObservation.awaited(comms.route) as process:
        assert (await process.read(ReadNotifications(str(comms.root), LiveNotificationRead(())))).outcomes == {}
    assert process.connection.closed and process.peer.process.exitcode == 0
    assert not process.reads


def test_local_pipe_corruption_is_not_peer_retirement(comms):
    import multiprocessing


    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        connection.close()
        with pytest.raises(OSError):
            service.send(NothingAnswered(1))
        assert not service.closed


async def test_busy_notification_observation_retries_without_revision_change(tmp_path, monkeypatch):
    """A notification read deferred by transient store contention must retry via retry_busy
    once contention clears, without stranding on Checking until an unrelated store commit."""
    comms, _session = saved_history(tmp_path)
    sender = comms.messaging.user_identity(Checkout(tmp_path)).name
    sent = comms.messaging.send("saved", sender, "ping").message
    original_apply = LiveNotificationRead.apply
    attempts = 0

    def busy_once(self, bus_comms, reader):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CoordinationReadUnavailable("transient contention")
        return original_apply(self, bus_comms, reader)

    monkeypatch.setattr(LiveNotificationRead, "apply", busy_once)
    request = ReadNotifications(str(comms.root), LiveNotificationRead((sent.reference,)))
    client, server = Pipe()
    with client, server:
        service = ObservationService(comms, server)
        service.accept(ObserveNotifications(frozenset((request,))))
        runner = asyncio.create_task(service.run())
        try:
            async with asyncio.timeout(5):
                while True:
                    if client.poll(0.05):
                        item = client.recv()
                        if isinstance(item, NotificationsObserved) and (
                            sent.reference.seq, sent.reference.message_id
                        ) in item.outcomes:
                            break
                    await asyncio.sleep(0.02)
            assert attempts == 2
        finally:
            service.retire()
            await asyncio.wait_for(runner, timeout=5)
            comms.views.transcript_reads.close()


