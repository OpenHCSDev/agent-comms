"""A frame relay ends both sides when either ends, and never blocks its event loop."""

import asyncio
import socket

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.ui_model.observation import RelayConnection, relay_frames


def relaying(local: RelayConnection) -> TestServer:
    """A WebSocket endpoint that relays its socket to ``local``."""

    async def observe(request: web.Request) -> web.WebSocketResponse:
        peer = web.WebSocketResponse()
        await peer.prepare(request)
        await relay_frames(local, peer)
        return peer

    application = web.Application()
    application.router.add_get("/observe", observe)
    return TestServer(application)


async def test_the_local_side_ending_closes_the_socket() -> None:
    near, far = socket.socketpair()
    async with relaying(RelayConnection(near.detach())) as server, TestClient(server) as client:
        viewer = await client.ws_connect("/observe")
        far.close()
        message = await asyncio.wait_for(viewer.receive(), 10)
        assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)


async def test_the_peer_closing_ends_the_local_connection() -> None:
    near, far = socket.socketpair()
    with far:
        async with relaying(RelayConnection(near.detach())) as server, TestClient(server) as client:
            viewer = await client.ws_connect("/observe")
            await viewer.close()
            far.settimeout(10)
            assert await asyncio.to_thread(far.recv, 1) == b""


async def test_a_local_side_that_stops_reading_never_blocks_the_loop() -> None:
    near, far = socket.socketpair()
    ticks = 0

    async def ticking() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    async with relaying(RelayConnection(near.detach())) as server, TestClient(server) as client:
        viewer = await client.ws_connect("/observe")
        ticker = asyncio.create_task(ticking())
        # Far never reads: the socket buffer fills and the relay's write waits in its thread.
        for _ in range(64):
            await viewer.send_bytes(b"x" * 65536)
        before = ticks
        await asyncio.sleep(0.5)
        ticker.cancel()
        assert ticks > before + 10, "the event loop stalled behind a blocked socket write"
        far.close()
        message = await asyncio.wait_for(viewer.receive(), 10)
        assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)


class CountedConnection(RelayConnection):
    """Counts socket calls that entered and that returned, in the relay's worker threads.
    Its write deadline is short, so a test needs not wait the production 30 seconds."""

    write_timeout_seconds = 1

    def __init__(self, handle: int) -> None:
        super().__init__(handle)
        self.entered = self.returned = 0

    def recv_bytes(self, maxlength=None):
        self.entered += 1
        try:
            return super().recv_bytes(maxlength)
        finally:
            self.returned += 1

    def send_bytes(self, buf, offset=0, size=None):
        self.entered += 1
        try:
            return super().send_bytes(buf, offset, size)
        finally:
            self.returned += 1


async def test_ending_retires_a_write_blocked_on_a_full_socket() -> None:
    """The relay's end returns every worker thread, including one blocked in send_bytes."""
    near, far = socket.socketpair()
    local = CountedConnection(near.detach())
    async with relaying(local) as server, TestClient(server) as client:
        viewer = await client.ws_connect("/observe")
        for _ in range(64):
            await viewer.send_bytes(b"x" * 65536)
        await asyncio.sleep(0.3)
        assert local.entered > local.returned, "no socket call was blocked"
        await viewer.close()
        for _ in range(100):
            if local.entered == local.returned:
                break
            await asyncio.sleep(0.05)
        assert local.entered == local.returned, "a worker thread is still inside a socket call"
    far.close()
