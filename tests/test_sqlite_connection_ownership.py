"""SQLite connection and busy policy have one production owner."""

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore, StoreBusy
from agent_comms.coordination_errors import CoordinationReadUnavailable
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
        writer.session.transaction(),
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
            with store.session.transaction() as db:
                db.execute("UPDATE participants SET display_name = 'Updated' WHERE participant_lookup = 'first'")
        await Coordination.run_worker(lambda: Coordination._run_owned(
            path, rename, None, StoreBusy()))
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
