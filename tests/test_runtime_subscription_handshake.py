"""Current subscription contract exercised across an actual owner Unix socket."""

import asyncio
import json
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


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
    comms.threads.register(
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
