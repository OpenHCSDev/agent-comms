"""SQLite connection and busy policy have one production owner."""

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore, StoreBusy
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.coordination_session import CoordinationSession, system_clock
from agent_comms.coordinator import Coordination

PACKAGE = Path(__file__).parents[1] / "src" / "agent_comms"
CONNECTION_OWNER = PACKAGE / "coordination_database.py"


def test_sqlite_connections_and_busy_policy_have_one_owner():
    direct_connections = []
    timeout_arguments = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.arg == "lock_timeout":
                timeout_arguments.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
            if not isinstance(node, ast.Call):
                continue
            if (
                path != CONNECTION_OWNER
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
                and node.func.attr == "connect"
            ):
                direct_connections.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
            if any(keyword.arg == "lock_timeout" for keyword in node.keywords):
                timeout_arguments.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert direct_connections == []
    assert timeout_arguments == []


@pytest.mark.asyncio
async def test_scoped_reader_releases_for_owner_commit(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with CoordinationStore(path):
        pass
    acquired = threading.Event()
    release = threading.Event()

    def read(store):
        with store.session.read():
            store.session._connection.execute("SELECT schema_version FROM schema_meta").fetchone()
            acquired.set()
            assert release.wait(2)

    def commit(store):
        store.participants.register(
            "reader-release-owner", "Reader Release Owner", "owner-thread"
        )

    read_task = asyncio.create_task(Coordination.run_read_async(path, read))
    assert await asyncio.to_thread(acquired.wait, 2)
    write_task = asyncio.create_task(Coordination.run_async(path, commit))
    await asyncio.sleep(0.05)
    try:
        assert not write_task.done()
    finally:
        release.set()
    await asyncio.gather(read_task, write_task)


@pytest.mark.asyncio
async def test_nonwaiting_reader_reports_named_store_busy(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    def read(store):
        with store.session.read():
            pytest.fail("busy snapshot reached its consumer")

    with (
        Coordination(str(path)) as writer,
        writer.session.writing(),
        pytest.raises(CoordinationReadUnavailable),
    ):
        await Coordination.run_read_async(path, read, contention=StoreBusy())


@pytest.mark.asyncio
async def test_resident_connections_keep_thread_affinity_and_fresh_snapshots(tmp_path, monkeypatch):
    from agent_comms.coordination_session import CoordinationSession

    path = tmp_path / "coordination.sqlite3"
    opened, closed = [], []
    configure = CoordinationStore._configure_connection
    close = CoordinationSession.close

    def configured(store):
        opened.append(threading.get_ident())
        return configure(store)

    def closing(store):
        closed.append(threading.get_ident())
        return close(store)

    monkeypatch.setattr(CoordinationStore, "_configure_connection", configured)
    monkeypatch.setattr(CoordinationSession, "close", closing)
    writer_ids, reader_ids, threads = set(), set(), set()

    def write(store):
        writer_ids.add(id(store.session._connection))
        threads.add(threading.get_ident())
        store.participants.register("first", "First", "first")

    def read(store):
        reader_ids.add(id(store.session._connection))
        threads.add(threading.get_ident())
        with store.session.read():
            return store.participants.get("first").display_name

    async with Coordination.worker(path):
        await Coordination.run_async(path, write)
        assert await Coordination.run_read_async(path, read) == "First"
        # A separate connection commits between operations. A resident reader
        # must acquire a NEW snapshot, not keep its previous read transaction.
        def rename(store):
            with store.session.writing() as db:
                db.execute("UPDATE participants SET display_name = 'Updated' WHERE participant_lookup = 'first'")
        await Coordination.run_worker(lambda: Coordination._run_owned(
            path, rename, system_clock, StoreBusy()))
        assert await Coordination.run_read_async(path, read) == "Updated"
        await Coordination.run_async(path, write)
        assert len(writer_ids) == len(reader_ids) == len(threads) == 1
        assert writer_ids.isdisjoint(reader_ids)
        assert threading.get_ident() not in threads
        assert not closed or all(thread not in threads for thread in closed)
    # Both resident stores close on their owning thread, after the scope ends.
    assert closed.count(next(iter(threads))) == 2
    assert opened.count(next(iter(threads))) == 1  # reader config is query-only


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", [False, True])
async def test_resident_operation_releases_unfinished_transactions(tmp_path, failing):
    path = tmp_path / "coordination.sqlite3"
    def unfinished(store):
        store.session._connection.execute("BEGIN")
        if failing:
            raise ValueError("callback failed")
    async with Coordination.worker(path):
        for run in (Coordination.run_async, Coordination.run_read_async):
            if failing:
                with pytest.raises(ValueError, match="callback failed"):
                    await run(path, unfinished)
            else:
                await run(path, unfinished)
            assert not await run(path, lambda store: store.session._connection.in_transaction)


@pytest.mark.asyncio
async def test_resident_cancellation_joins_operation_before_retirement(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    begun, release, finished = threading.Event(), threading.Event(), threading.Event()
    def operation(store):
        begun.set()
        assert release.wait(2)
        store.participants.register("joined", "Joined", "joined")
        finished.set()
    async with Coordination.worker(path):
        pending = asyncio.create_task(Coordination.run_async(path, operation))
        assert await asyncio.to_thread(begun.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finished.is_set()
        assert await Coordination.run_read_async(path, lambda store: store.participants.get("joined").owner_thread) == "joined"


@pytest.mark.asyncio
async def test_resident_initializer_failure_closes_partial_store(tmp_path, monkeypatch):
    from concurrent.futures.thread import BrokenThreadPool
    from agent_comms.coordination_session import CoordinationSession, ReadNativeCoordinationSession

    closed = []
    close = CoordinationSession.close

    def closing(session):
        closed.append(threading.get_ident())
        return close(session)

    def fail_reader(*args, **kwargs):
        raise OSError("reader initialization failed")

    monkeypatch.setattr(CoordinationSession, "close", closing)
    monkeypatch.setattr(ReadNativeCoordinationSession, "__init__", fail_reader)
    with pytest.raises(BrokenThreadPool):
        async with Coordination.worker(tmp_path / "coordination.sqlite3"):
            await Coordination.run_async(tmp_path / "coordination.sqlite3", lambda store: None)
    assert len(closed) == 1
    assert closed[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_clock_default_and_explicit_clock_follow_session_owner(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    before = system_clock()
    assert await Coordination.run_async(path, lambda store: store.session.now()) >= before
    assert await Coordination.run_read_async(path, lambda store: store.session.now(), clock_ms=lambda: 47) == 47
    with CoordinationSession(str(path), clock_ms=lambda: 31) as session:
        assert session.now() == 31
    async with Coordination.worker(path):
        assert await Coordination.run_async(path, lambda store: store.session.now()) >= before
        with pytest.raises(ValueError, match="resident coordination owner"):
            await Coordination.run_read_async(path, lambda store: store.session.now(), clock_ms=lambda: 47)


@pytest.mark.asyncio
async def test_nested_resident_scopes_restore_outer_and_standalone_routes(tmp_path):
    outer, inner = tmp_path / "outer.sqlite3", tmp_path / "inner.sqlite3"

    async def register(path, name):
        await Coordination.run_async(path, lambda store: store.participants.register(name, name, name))

    async def owner(path, name):
        return await Coordination.run_read_async(path, lambda store: store.participants.get(name).owner_thread)

    async with Coordination.worker(outer):
        await register(outer, "outer")
        async with Coordination.worker(inner):
            await register(inner, "inner")
            assert await owner(inner, "inner") == "inner"
            with pytest.raises(ValueError, match="resident coordination owner"):
                await owner(outer, "outer")
        assert await owner(outer, "outer") == "outer"
    # Outside both scopes, either root uses a fresh per-operation store again.
    assert await owner(inner, "inner") == "inner"
    assert await owner(outer, "outer") == "outer"
