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
