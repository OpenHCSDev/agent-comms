"""A session on another machine is woken by a pushed frame, through the login it already has."""

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from agent_comms.comms import Comms
from agent_comms.connector import Connector
from agent_comms.connector_store import ConnectorStore
from agent_comms.repo import Checkout
from agent_comms.thread_status import ConnectedThreadStatus, NoBridgeThreadStatus
from agent_comms.threads import ExternalThread, NativeThread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsInboxTool


@pytest.fixture
async def session(tmp_path: Path, comms: Comms):
    """A live connector over a real store, holding the Director login a claude.ai session has."""
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    connector = Connector(store, "http://127.0.0.1:18767", comms.root)
    async with TestServer(connector.application()) as server:
        async with TestClient(server) as client:
            with store.granted("test") as token:
                yield client, token


async def watch_path(client: TestClient, token: str, name: str) -> str:
    """The URL the tool hands the model, as the path this test server's client connects to."""
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "comms_watch", "arguments": {"fork": {"name": name}}}})
    reply = await response.json()
    assert not reply["result"]["isError"], reply
    url = URL(reply["result"]["structuredContent"]["url"])
    assert url.scheme == "ws"
    return url.path


async def frame(socket) -> dict:
    return json.loads(await asyncio.wait_for(socket.receive_str(), timeout=30))


async def inbox_emptied(comms: Comms, thread: str) -> None:
    """A frame goes out before its message is recorded as taken, so a test waits for the record."""
    request = CommsInboxTool(thread=thread)
    async with asyncio.timeout(10):
        while request.apply(comms, ThreadCaller(thread)).messages:
            await asyncio.sleep(0.05)


async def test_a_pushed_frame_arrives_and_a_rearmed_watch_does_not_replay_it(
    session, comms: Comms,
) -> None:
    """Both are waking: a message already waiting and one sent while the socket is open."""
    client, token = session
    path = await watch_path(client, token, "cloud")
    comms.messaging.send("sender", "cloud", "first")
    socket = await client.ws_connect(path)
    assert (await frame(socket))["text"] == "first"
    comms.messaging.send("sender", "cloud", "second")
    assert (await frame(socket))["text"] == "second"
    await inbox_emptied(comms, "cloud")
    await socket.close()

    comms.messaging.send("sender", "cloud", "third")
    again = await client.ws_connect(await watch_path(client, token, "cloud"))
    assert (await frame(again))["text"] == "third"
    await again.close()


async def test_the_thread_reads_as_bridged_only_while_its_socket_is_open(
    session, comms: Comms,
) -> None:
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))

    async def status_is(status: type) -> None:
        async with asyncio.timeout(10):
            while not isinstance(comms.registry.snapshot().entry("cloud").status, status):
                await asyncio.sleep(0.05)

    await status_is(ConnectedThreadStatus)
    await socket.close()
    await status_is(NoBridgeThreadStatus)


async def test_a_watch_url_opens_once(session) -> None:
    client, token = session
    path = await watch_path(client, token, "cloud")
    socket = await client.ws_connect(path)
    with pytest.raises(WSServerHandshakeError):
        await client.ws_connect(path)
    await socket.close()


async def test_a_watch_url_is_no_login(session) -> None:
    """The ticket opens its socket and nothing else: the connector does not know it as a bearer."""
    client, token = session
    ticket = URL(await watch_path(client, token, "cloud")).name
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {ticket}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status == 401


async def test_a_newer_socket_replaces_the_older_one(session, comms: Comms) -> None:
    client, token = session
    older = await client.ws_connect(await watch_path(client, token, "cloud"))
    newer = await client.ws_connect(await watch_path(client, token, "cloud"))
    await asyncio.wait_for(older.receive(), timeout=10)
    assert older.closed
    comms.messaging.send("sender", "cloud", "after")
    assert (await frame(newer))["text"] == "after"
    await newer.close()
