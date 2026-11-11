"""Current subscription contract exercised across an actual owner Unix socket."""

import asyncio
import json
import logging
import os
import sqlite3

import pytest

from agent_comms.execution.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.owner_protocol import OwnerAnnouncement
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.nominal.field_codec import AcpSessionId, MalformedRecord
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.private_path import PrivateSocketRole
from agent_comms.transport.owner_connection import NoControllerToken, socket_path

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


async def test_subscription_busy_read_retains_original_traceback(tmp_path, caplog):
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, auto_wake=False)
    client = CommsAgent(comms, auto_wake=False)
    session_file = tmp_path / "owner.jsonl"
    session_file.write_text(json.dumps({
        "type": "session", "version": 3, "id": "owner-session",
        "timestamp": "2026-10-10T00:00:00.000Z", "cwd": str(tmp_path),
    }) + "\n")
    session_file.chmod(0o600)
    thread = NativeThread('owner', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), session_file=RegistrySessionLocation.from_path(str(session_file), comms.root))
    comms.registry.declare(thread)
    await owner.sessions.bind_owned(thread, AcpSessionId(thread.name))
    database = comms.root / "coordination.sqlite3"
    CoordinationStore(database).close()
    proxy = client._create_runtime_proxy(thread, AcpSessionId(thread.name))
    holder = sqlite3.connect(database, isolation_level=None)
    try:
        with caplog.at_level(logging.ERROR, logger="agent_comms.execution.runtime"):
            # Hold the coordinator's actual exclusive transaction. The real
            # subscription must refuse its transcript read before ready; that
            # read runs in the observation process, which reports its own
            # traceback across its pipe.
            holder.execute("BEGIN EXCLUSIVE")
            try:
                with pytest.raises(RuntimeError):
                    await proxy.subscribe()
            finally:
                holder.execute("ROLLBACK")
        failures = [record for record in caplog.records if record.exc_info]
        assert len(failures) == 1
        assert isinstance(failures[0].exc_info[1], CoordinationReadUnavailable)
        text = logging.Formatter().formatException(failures[0].exc_info)
        assert "SQLITE_BUSY" in text
        assert str(database) in text
        assert proxy.task is None and proxy.controller_token == NoControllerToken()
        assert not owner._runtime.clients.get(thread.name)
        assert not (comms.root / "input_dispositions.sqlite3").exists()
        assert not (comms.root / "native-sessions").exists()
    finally:
        holder.close()
        await proxy.close()
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.parametrize(
    "token_fields",
    [{}, {"controllerToken": None}, {"controllerToken": 42}, {"controllerToken": ""},
     {"controllerToken": "a" * 63}, {"controllerToken": "a" * 65}],
    ids=["missing", "null", "number", "empty", "short", "long"],
)
async def test_subscription_rejects_invalid_ready_and_closes_socket(tmp_path, token_fields):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(
        NativeThread('owner', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    path = socket_path(comms.root, os.getpid())
    path.parent.mkdir(parents=True, exist_ok=True)
    finished = asyncio.get_running_loop().create_future()

    async def reply(reader, writer):
        try:
            request = json.loads(await reader.readline())
            assert request["action"] == "subscribe" and request["thread"] == "owner"
            writer.write(OwnerAnnouncement().wire())
            ready = {"kind": "owner_ready", "ready": {}, "configOptions": [], **token_fields}
            writer.write((json.dumps(ready) + "\n").encode())
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

    with PrivateSocketRole.address(path) as address:
        server = await asyncio.start_unix_server(reply, path=address)
    client = CommsAgent(comms, auto_wake=False)
    proxy = client._create_runtime_proxy(comms.registry.require("owner"), AcpSessionId("owner"))
    try:
        async with asyncio.timeout(3):
            with pytest.raises(MalformedRecord):
                await proxy.subscribe()
            await finished
        assert proxy._controller_token == NoControllerToken()
        assert proxy.task is None
        assert proxy.writer.is_closing()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)
        await client.shutdown()


