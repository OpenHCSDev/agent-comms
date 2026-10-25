"""SQLite connection and busy policy have one production owner."""

import ast
import asyncio
import inspect
import threading
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.coordination_session import CoordinationSession, system_clock
from agent_comms.coordinator import Coordination
from agent_comms.store_files import StoreBusy, StoreContention
from bus_record_fixture import OWNER_LOOKUP, RECIPIENT_LOOKUP, SENDER_LOOKUP

PACKAGE = Path(__file__).parents[1] / "src" / "agent_comms"
CONNECTION_OWNER = Path(inspect.getfile(StoreContention))


def test_sqlite_connections_and_busy_policy_have_one_owner():
    direct_connections = []
    timeout_arguments = []
    concrete_dependencies = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (path == CONNECTION_OWNER and isinstance(node, ast.ImportFrom)
                    and any(name.name == "CoordinationStore" for name in node.names)):
                concrete_dependencies.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
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
    assert concrete_dependencies == []


@pytest.mark.asyncio
async def test_scoped_reader_releases_for_owner_commit(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with CoordinationStore(path):
        pass
    acquired = threading.Event()
    release = threading.Event()

    def read(store):
        with store.session.read():
            store.session._connection.execute("SELECT count(*) FROM participants").fetchone()
            acquired.set()
            assert release.wait(2)

    def commit(store):
        store.participants.register(
            OWNER_LOOKUP
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
                with pytest.raises(ValueError):
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
        store.participants.register(OWNER_LOOKUP)
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
        assert await Coordination.run_read_async(
            path, lambda store: store.participants.get(OWNER_LOOKUP).owner_identity.owner_lookup
        ) == OWNER_LOOKUP


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
        with pytest.raises(ValueError):
            await Coordination.run_read_async(path, lambda store: store.session.now(), clock_ms=lambda: 47)


@pytest.mark.asyncio
async def test_nested_resident_scopes_restore_outer_and_standalone_routes(tmp_path):
    outer, inner = tmp_path / "outer.sqlite3", tmp_path / "inner.sqlite3"

    async def register(path, name):
        await Coordination.run_async(path, lambda store: store.participants.register(name))

    async def owner(path, name):
        return await Coordination.run_read_async(path, lambda store: store.participants.get(name).owner_identity.owner_lookup)

    async with Coordination.worker(outer):
        await register(outer, SENDER_LOOKUP)
        async with Coordination.worker(inner):
            await register(inner, RECIPIENT_LOOKUP)
            assert await owner(inner, RECIPIENT_LOOKUP) == RECIPIENT_LOOKUP
            with pytest.raises(ValueError):
                await owner(outer, SENDER_LOOKUP)
        assert await owner(outer, SENDER_LOOKUP) == SENDER_LOOKUP
    # Outside both scopes, either root uses a fresh per-operation store again.
    assert await owner(inner, RECIPIENT_LOOKUP) == RECIPIENT_LOOKUP
    assert await owner(outer, SENDER_LOOKUP) == SENDER_LOOKUP
