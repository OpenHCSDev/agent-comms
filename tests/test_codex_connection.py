"""Decoded replies deliver their own meaning without a second type switch."""
import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import aiohttp
import pytest

from agent_comms.cli_bridges.codex import CodexConnection
from agent_comms.cli_bridges.codex_protocol import Capabilities, ClientInfo, Initialize


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def send_str(self, data):
        self.sent.append(json.loads(data))

    async def receive(self):
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(next(self.frames)))


@pytest.mark.parametrize(('frames', 'outcome'), [
    ([{'method': 'notice', 'params': {}}, {'id': 1, 'result': {'ok': True}}], nullcontext({'ok': True})),
    ([{'id': 1, 'error': {'code': -1, 'message': 'bad'}}], pytest.raises(RuntimeError)),
    ([{'id': None, 'error': {'code': -1, 'message': 'bad'}}], pytest.raises(ConnectionError)),
    ([{'id': 7, 'result': {}}], pytest.raises(ValueError)),
])
async def test_codex_call_routes_the_parsed_reply(frames, outcome):
    connection = CodexConnection(Socket(frames))
    with outcome as expected:
        assert await connection.call(Initialize(clientInfo=ClientInfo(), capabilities=Capabilities())) == expected
        assert (await anext(connection.notifications())).method == 'notice'
    assert not connection.answers


async def test_codex_notifications_reject_an_unrequested_answer():
    connection = CodexConnection(Socket([{'id': 1, 'result': {}}]))
    with pytest.raises(ValueError):
        await anext(connection.notifications())


async def test_codex_cancelled_call_releases_its_answer():
    entered = asyncio.Event()

    class WaitingSocket(Socket):
        async def receive(self):
            entered.set()
            await asyncio.Future()

    connection = CodexConnection(WaitingSocket([]))
    task = asyncio.create_task(connection.call(Initialize(clientInfo=ClientInfo(), capabilities=Capabilities())))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not connection.answers
