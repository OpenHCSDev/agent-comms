"""Source contention retains a viewer read, never manufactures an input failure."""

import asyncio
import json
import subprocess
import sys
from contextlib import contextmanager

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.coordinator import Coordination
from agent_comms.threads import Thread
from agent_comms.ui_model.observation import (
    ObservationProcess, ReadBusy, ReadNotifications, ReadTranscript,
)
from agent_comms.thread_identity import UnrecordedThreadIncarnation


def saved_history(tmp_path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "saved.jsonl"
    timestamp = "2026-10-20T00:00:00.000Z"
    session.write_text("".join(json.dumps(record) + "\n" for record in (
        {"type": "session", "version": 3, "id": "saved-session", "timestamp": timestamp},
        {"type": "message", "id": "saved-answer", "parentId": None, "timestamp": timestamp,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Saved answer"}]}},
    )))
    comms.registry.declare(comms.registry.new_thread('saved', frozenset(), str(tmp_path), session_file=str(session)))
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.install_private_runtime()
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
    with ObservationProcess.awaited(comms.route) as process:
        with held_source(comms):
            answer = ReadTranscript(str(comms.root), "saved", request_id=19).answer(comms)
            assert isinstance(answer, ReadBusy)
            assert answer.title == "History loading"
            waiting = asyncio.create_task(process.read(ReadTranscript(str(comms.root), "saved")))
            independent = asyncio.create_task(process.read(ReadNotifications(str(comms.root), ())))
            async with asyncio.timeout(15):
                assert await independent == {}
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


async def test_startup_replay_uses_retained_read_not_delivery_error(tmp_path):
    comms, session = saved_history(tmp_path)
    original = session.read_bytes()
    agent = CommsAgent(comms)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    task = None
    try:
        with held_source(comms):
            task = asyncio.create_task(agent.sessions.transcript.replay("saved", client=Client()))
            async with asyncio.timeout(15):
                while "reads" not in agent.sessions.transcript.__dict__:
                    await asyncio.sleep(0.01)
                process = agent.sessions.transcript.reads
                while not process.reads:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.15)
            assert not task.done()
            assert updates == []  # Not an empty-history update or delivery/resend error.
        async with asyncio.timeout(5):
            await task
        snapshot, = (value for value in decode_updates(updates[0].field_meta)
                     if isinstance(value, TranscriptSnapshotUpdate))
        assert [event.text for event in snapshot.page.events] == ["Saved answer"]
        assert snapshot.identity.runtime.read_key == str(session)
        assert session.read_bytes() == original
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await agent.shutdown()


async def test_cancelled_busy_read_is_removed_without_resubmission(tmp_path):
    comms, _session = saved_history(tmp_path)
    with ObservationProcess.awaited(comms.route) as process:
        with held_source(comms):
            waiting = asyncio.create_task(process.read(ReadTranscript(str(comms.root), "saved")))
            await asyncio.sleep(0.15)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert process.reads == {}
        async with asyncio.timeout(5):
            assert await process.read(ReadNotifications(str(comms.root), ())) == {}
        assert process.reads == {}


async def test_retirement_detaches_and_settles_reads_before_one_pipe_close(tmp_path, monkeypatch):
    from multiprocessing.connection import Connection
    from agent_comms.coordination_errors import StaleRevision
    from agent_comms.ui_model.observation import StopSidebar

    comms, session = saved_history(tmp_path)
    original = session.read_bytes()
    loop = asyncio.get_running_loop()
    remove = loop.remove_reader
    close = Connection.close
    events = []

    def removed(fd):
        if fd == descriptor:
            events.append("detach")
        return remove(fd)

    def closed(connection):
        if connection is process.connection:
            assert process.reads == {}
            events.append("close")
        return close(connection)

    # Register the actual loop method after instrumenting it; no replacement
    # pipe, child, source result or lifecycle implementation.
    monkeypatch.setattr(loop, "remove_reader", removed)
    with ObservationProcess.awaited(comms.route) as process:
        descriptor = process.fileno()
        monkeypatch.setattr(Connection, "close", closed)
        with held_source(comms):
            waiting = asyncio.create_task(process.read(ReadTranscript(str(comms.root), "saved")))
            await asyncio.sleep(0.15)
            assert process.reads and not waiting.done()
    assert events == ["detach", "close"]
    with pytest.raises(StaleRevision, match="retired before the read"):
        await waiting
    process.close()
    assert events == ["detach", "close"]
    assert process.connection.closed and process.process.exitcode == 0
    assert process.results() == []
    process.request(StopSidebar())
    with pytest.raises(StaleRevision, match="retired Comms observation"):
        await process.read(ReadNotifications(str(comms.root), ()))
    assert session.read_bytes() == original


@pytest.mark.parametrize("error", [GeneratorExit(), asyncio.CancelledError(), RuntimeError("original caller failure")])
async def test_awaited_scope_preserves_exit_and_closes_once(comms, monkeypatch, error):
    from multiprocessing.connection import Connection

    process = ObservationProcess.awaited(comms.route)
    original = Connection.close
    closes = []

    def closed(connection):
        if connection is process.connection:
            closes.append(connection)
        return original(connection)

    monkeypatch.setattr(Connection, "close", closed)
    with pytest.raises(type(error)) as raised:
        with process:
            raise error
    assert raised.value is error
    process.close()
    assert closes == [process.connection]
    assert process.connection.closed and process.process.exitcode == 0


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
        ObservationProcess(comms.route)
    assert raised.value is failure
    assert len(closed) == 2 and all(connection.closed for connection in closed)
    assert endpoints and endpoints[0] in closed


def test_viewer_reset_with_unread_observations_ends_the_service(comms):
    import multiprocessing
    from agent_comms.ui_model.observation import ObservationService, RevisionObserved

    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        connection.send(RevisionObserved(comms.views.revision()))
        viewer.close()  # The unread observation makes the real socketpair reset.
        service.receive()
        assert service.closed and service.changed.is_set()


async def test_peer_write_failure_retires_once_and_cancels_owner_reads(comms):
    import multiprocessing

    from agent_comms.ui_model.observation import ObservationService, ReadAnswered

    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        asking = asyncio.create_task(asyncio.Event().wait())
        service.owner_reads[7] = asking
        viewer.close()
        # The real Connection pickler exports a BytesIO memoryview for this payload.
        service.send(ReadAnswered(1, "x" * 20_000))
        assert service.closed and service.changed.is_set()
        assert asking.cancelling() == 1
        service.retire()
        service.send(ReadAnswered(2, "unreachable"))
        service.receive()
        assert asking.cancelling() == 1
        with pytest.raises(asyncio.CancelledError):
            await asking


async def test_owner_reply_uses_the_same_peer_retirement(comms):
    import multiprocessing

    from agent_comms.ui_model.observation import ObservationService, ReadOwnerGoal

    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        # A real missing-owner refusal still has to be sent through the shared endpoint.
        request = ReadOwnerGoal(str(comms.root), UnrecordedThreadIncarnation("missing"), request_id=7)
        request.receive(service)
        asking = service.owner_reads[7]
        viewer.close()
        async with asyncio.timeout(5):
            await asking
        assert not service.owner_reads
        assert service.closed and service.changed.is_set()


async def test_awaited_notifications_then_immediate_close_retires_successfully(comms):
    with ObservationProcess.awaited(comms.route) as process:
        assert await process.read(ReadNotifications(str(comms.root), ())) == {}
    assert process.connection.closed and process.process.exitcode == 0
    assert not process.reads


def test_local_pipe_corruption_is_not_peer_retirement(comms):
    import multiprocessing

    from agent_comms.ui_model.observation import ObservationService, ReadAnswered

    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        connection.close()
        with pytest.raises(OSError, match="handle is closed"):
            service.send(ReadAnswered(1, None))
        assert not service.closed


def test_other_write_errors_are_not_reclassified_as_peer_retirement(comms, monkeypatch):
    import multiprocessing

    from agent_comms.ui_model.observation import ObservationService, ReadAnswered

    connection, viewer = multiprocessing.get_context("spawn").Pipe()
    with connection, viewer:
        service = ObservationService(comms, connection)
        error = ConnectionResetError("not the observation pipe's EPIPE")

        def failed_write(_result):
            raise error

        monkeypatch.setattr(connection, "send", failed_write)
        with pytest.raises(ConnectionResetError) as raised:
            service.send(ReadAnswered(1, None))
        assert raised.value is error and not service.closed
