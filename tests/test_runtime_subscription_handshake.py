"""Current subscription contract exercised across an actual owner Unix socket."""

import asyncio
import json
import os
import logging
import sqlite3

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.coordination_database import CoordinationStore
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


async def test_subscription_busy_read_retains_original_traceback(tmp_path, caplog):
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, runtime_enabled=True, auto_wake=False)
    client = CommsAgent(comms, auto_wake=False)
    session_file = tmp_path / "owner.jsonl"
    session_file.write_text(json.dumps({
        "type": "session", "version": 3, "id": "owner-session",
        "timestamp": "2026-10-10T00:00:00.000Z", "cwd": str(tmp_path),
    }) + "\n")
    session_file.chmod(0o600)
    thread = Thread("owner", frozenset(), str(tmp_path),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                    session_file=str(session_file))
    comms.registry.declare(thread)
    await owner.sessions.bind_owned(thread, thread.name)
    database = comms.root / "coordination.sqlite3"
    CoordinationStore(database).close()
    proxy = RuntimeProxy(client, thread.name, socket_path(comms.root, os.getpid()))
    holder = sqlite3.connect(database, isolation_level=None)
    try:
        with caplog.at_level(logging.ERROR, logger="agent_comms.runtime"):
            # Hold the coordinator's actual exclusive transaction. The real
            # subscription must refuse its transcript read before ready.
            holder.execute("BEGIN EXCLUSIVE")
            try:
                with pytest.raises(RuntimeError, match="^The original database read is busy$"):
                    await proxy.subscribe()
            finally:
                holder.execute("ROLLBACK")
        failures = [record for record in caplog.records if record.exc_info]
        assert len(failures) == 1
        error = failures[0].exc_info[1]
        assert error.__cause__.sqlite_errorname == "SQLITE_BUSY"
        assert any(str(database) in note for note in error.__cause__.__notes__)
        text = logging.Formatter().formatException(failures[0].exc_info)
        assert "runtime_requests.py" in text
        assert "transcript_updates.py" in text
        assert "native_runtime_input.py" in text
        assert "SELECT name FROM sqlite_master LIMIT 1" in text
        assert str(database) in text
        assert proxy.task is None and proxy.controller_token is None
        assert not owner._runtime.clients.get(thread.name)
        assert not (comms.root / "input_dispositions.json").exists()
        assert not (comms.root / "native-sessions").exists()
    finally:
        holder.close()
        await proxy.close()
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.parametrize(
    "token_fields",
    [
        {},
        {"controllerToken": None},
        {"controllerToken": 42},
        {"controllerToken": ""},
        {"controllerToken": "a" * 63},
        {"controllerToken": "a" * 65},
    ],
    ids=["missing", "null", "number", "empty", "short", "long"],
)
async def test_subscription_rejects_invalid_ready_and_closes_socket(tmp_path, token_fields):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    path = socket_path(comms.root, os.getpid())
    path.parent.mkdir(parents=True, exist_ok=True)
    finished = asyncio.get_running_loop().create_future()

    async def reply(reader, writer):
        try:
            request = json.loads(await reader.readline())
            assert request["action"] == "subscribe" and request["thread"] == "owner"
            writer.write((json.dumps({"ready": {}, **token_fields}) + "\n").encode())
            await writer.drain()
            # Rejection must close the attachment without sending a prompt or
            # accepting a permission-controller downgrade.
            assert await reader.read() == b""
        except BaseException as error:
            finished.set_exception(error)
        else:
            finished.set_result(None)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(reply, path=path)
    client = CommsAgent(comms, auto_wake=False)
    proxy = RuntimeProxy(client, "owner", path)
    try:
        async with asyncio.timeout(3):
            with pytest.raises(ValueError, match="ready requires a valid controllerToken"):
                await proxy.subscribe()
            await finished
        assert proxy._controller_token is None
        assert proxy.task is None
        assert proxy.writer.is_closing()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)
        await client.shutdown()
