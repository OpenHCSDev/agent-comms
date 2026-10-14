#!/home/ts/.local/share/agent-comms/runtimes/toad-86d9e5c49-core-ccec96b85-native-dba4356e87520876-20261014T165633Z/bin/python
"""Codex transport for the shared Core CLI bridge."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import aiohttp
from pydantic import JsonValue
from agent_comms.messages import Message
from cli_comms import TranscriptBridge, Options, Outcome, SendResult, TranscriptUpdate, shared_parser, parse_options, run_bridge
from codex_comms_protocol import (
    Capabilities, ClientInfo, DisplayOutput, Initialize, Initialized, MessageInput,
    Request, RPC_RESPONSE, RpcFailure, RpcNotification, RpcSuccess, TextInput,
    ToolOutput, RequestParams, ResumeThread, ItemsList, ItemsPage, ItemStream, BusInput,
)


class CodexConnection:
    """One WebSocket owns JSON-RPC initialization and response matching."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse, thread: str) -> None:
        self.socket = socket
        self.thread = thread
        self.sequence = 0
        self.pending: deque[RpcNotification] = deque()

    async def receive(self) -> RpcSuccess | RpcFailure | RpcNotification:
        frame = await self.socket.receive()
        if frame.type is not aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f'Codex connection ended: {frame.type.name}')
        return RPC_RESPONSE.validate_json(frame.data)

    async def call(self, params: RequestParams) -> JsonValue:
        self.sequence += 1
        request = Request[type(params)](id=self.sequence, method=params.method, params=params)
        await self.socket.send_str(request.model_dump_json(by_alias=True))
        async with asyncio.timeout(15):
            while True:
                reply = await self.receive()
                if isinstance(reply, RpcNotification):
                    self.pending.append(reply)
                    continue
                if reply.id != request.id:
                    raise ValueError('Codex replied to an unknown request')
                if isinstance(reply, RpcFailure):
                    raise RuntimeError(f'Codex {params.method}: {reply.error.message}')
                return reply.result

    async def notifications(self) -> AsyncIterator[RpcNotification]:
        while True:
            if self.pending:
                yield self.pending.popleft()
                continue
            reply = await self.receive()
            if not isinstance(reply, RpcNotification):
                raise ValueError('Unrequested Codex response on transcript subscription')
            yield reply

    @classmethod
    @asynccontextmanager
    async def open(cls, path: str, thread: str) -> AsyncIterator[CodexConnection]:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=path)) as client:
            async with client.ws_connect('http://localhost/') as socket:
                connection = cls(socket, thread)
                await connection.call(Initialize(
                    clientInfo=ClientInfo(), capabilities=Capabilities()))
                await socket.send_str(Initialized().model_dump_json())
                yield connection

    async def message(self, text: str, message_id: str) -> None:
        await self.call(BusInput.for_message(self.thread, message_id, text))

    async def sent(self, result: SendResult, text: str) -> None:
        await self.call(DisplayOutput(
            threadId=self.thread, clientUserMessageId=f'comms-out:{result.id}',
            toolOutput=ToolOutput(output=text)))

@dataclass
class CodexOptions(Options):
    socket: str


class CodexBridge(TranscriptBridge[CodexOptions]):
    async def transcript(self) -> AsyncIterator[TranscriptUpdate]:
        async with CodexConnection.open(self.options.socket, self.binding.thread) as codex:
            await codex.call(ResumeThread(threadId=self.binding.thread))
            page = ItemsPage.model_validate(await codex.call(ItemsList(threadId=self.binding.thread)))
            # Seed only completed snapshots. An active item's deltas must be
            # assembled from its actual start, not appended to an overlapping
            # snapshot fetched concurrently with those deltas.
            completed = {(entry.turnId, entry.item.id) for entry in page.data
                         if entry.completedAtMs is not None}
            for entry in reversed(page.data):
                if (entry.turnId, entry.item.id) in completed:
                    yield entry.publication()
            stream = ItemStream(self.binding.thread)
            buffered = tuple(codex.pending)
            codex.pending.clear()
            for notification in buffered:
                update = stream.receive(notification)
                if update is not None and (update.turn_id, update.item_id) not in completed:
                    yield update
            async for notification in codex.notifications():
                update = stream.receive(notification)
                if update is not None:
                    yield update

    async def deliver(self, message: Message) -> Outcome:
        async with CodexConnection.open(self.options.socket, self.binding.thread) as codex:
            await codex.message(self.incoming_text(message), message.message_id)
        return Outcome.DELIVERED

    async def show_sent(self, result: SendResult, target: str, body: str) -> None:
        async with CodexConnection.open(self.options.socket, self.binding.thread) as codex:
            await codex.sent(result, self.sent_text(result, target, body))

    async def report_failure(self, text: str) -> None:
        async with CodexConnection.open(self.options.socket, self.binding.thread) as codex:
            await codex.call(MessageInput(
                threadId=self.binding.thread, clientUserMessageId=f'bridge-error:{uuid4()}',
                input=(TextInput(text=text),)))


def main() -> None:
    parser = shared_parser(__doc__, Path.home() / '.local/state/codex-comms-inbox', CodexBridge)
    parser.add_argument('--socket', default=str(Path.home() / '.codex/app-server-control/app-server-control.sock'))
    run_bridge(CodexBridge, parse_options(parser, CodexOptions))


if __name__ == '__main__':
    main()
