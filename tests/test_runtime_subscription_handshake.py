"""Current subscription contract exercised across an actual owner Unix socket."""

import asyncio
import json
import os
import logging

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


async def test_subscription_busy_read_retains_original_traceback(tmp_path, caplog):
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, runtime_enabled=True, auto_wake=False)
    client = CommsAgent(comms, auto_wake=False)
    thread = Thread("owner", frozenset(), str(tmp_path),
                    process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(thread)
    await owner.sessions.bind_owned(thread, thread.name)
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    proxy = RuntimeProxy(client, thread.name, socket_path(comms.root, os.getpid()))
    try:
        with caplog.at_level(logging.ERROR, logger="agent_comms.runtime"):
            # Hold the original journal's actual exclusive transaction. The
            # real subscription must refuse its transcript read before ready.
            with journal.transaction(retain_exclusion=True):
                with pytest.raises(RuntimeError, match="^The original database read is busy$"):
                    await proxy.subscribe()
        failures = [record for record in caplog.records if record.exc_info]
        assert len(failures) == 1
        error = failures[0].exc_info[1]
        assert error.__cause__.sqlite_errorname == "SQLITE_BUSY"
        assert any(str(journal.path) in note for note in error.__cause__.__notes__)
        text = logging.Formatter().formatException(failures[0].exc_info)
        assert "runtime_requests.py" in text
        assert "transcript_updates.py" in text
        assert "compaction_journal.py" in text
        assert "SELECT name FROM sqlite_master LIMIT 1" in text
        assert str(journal.path) in text
        assert proxy.task is None and proxy.controller_token is None
        assert not owner._runtime.clients.get(thread.name)
        assert not (comms.root / "input_dispositions.json").exists()
        assert not (comms.root / "native-sessions").exists()
    finally:
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
