"""The Codex app-server's JSON-RPC connection, shared by the bridge and account reads."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, ClassVar

import aiohttp
from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec
from .codex_protocol import (
    Capabilities, ClientInfo, Initialize, InitializeResponse, Initialized, Request,
    RPC_RESPONSE, RateLimits, RateLimitsResponse, ReadRateLimits, RequestParams, RpcNotification, RpcReply,
)


class CodexConnection:
    """One WebSocket owns JSON-RPC initialization and response matching."""

    CONTROL_SOCKET: ClassVar[Path] = Path.home() / '.codex/app-server-control/app-server-control.sock'

    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self.socket = socket
        self.sequence = 0
        self.pending: deque[RpcNotification] = deque()
        self.answers: dict[int, asyncio.Future[JsonValue]] = {}
        self.initialized: InitializeResponse

    def answer(self, request_id: int) -> asyncio.Future[JsonValue]:
        try:
            return self.answers[request_id]
        except KeyError as error:
            raise ValueError('Codex replied to an unknown request') from error

    async def receive(self) -> RpcReply:
        frame = await self.socket.receive()
        if frame.type is not aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f'Codex connection ended: {frame.type.name}')
        return RPC_RESPONSE.validate_json(frame.data)

    async def call[Params: RequestParams](self, params: Params) -> JsonValue:
        self.sequence += 1
        request = Request[Params](id=self.sequence, method=params.method, params=params)
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self.answers[request.id] = future
        try:
            async with asyncio.timeout(15):
                await self.socket.send_str(request.model_dump_json(by_alias=True))
                while not future.done():
                    (await self.receive()).route(self)
                return await future
        finally:
            del self.answers[request.id]
            future.cancel()

    async def notifications(self) -> AsyncIterator[RpcNotification]:
        while True:
            if self.pending:
                yield self.pending.popleft()
                continue
            (await self.receive()).route(self)

    @classmethod
    @asynccontextmanager
    async def open(cls, path: str) -> AsyncIterator[CodexConnection]:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=path)) as client:
            async with client.ws_connect('http://localhost/') as socket:
                connection = cls(socket)
                connection.initialized = InitializeResponse.model_validate(await connection.call(Initialize(
                    clientInfo=ClientInfo(), capabilities=Capabilities())))
                await socket.send_str(Initialized().model_dump_json())
                yield connection

    @classmethod
    async def rate_limits(cls) -> RateLimits:
        """Read account-wide usage from the running server, never from session logs."""
        async with cls.open(str(cls.CONTROL_SOCKET)) as codex:
            return FieldCodec.decode(
                RateLimitsResponse, await codex.call(ReadRateLimits()),
            ).rate_limits
