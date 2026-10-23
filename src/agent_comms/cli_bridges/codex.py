"""Codex sessions held by a running app-server, and the bridge that follows one."""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Self
from uuid import uuid4

import aiohttp
from pydantic import JsonValue
from agent_comms.messages import Message
from agent_comms.send_result import SendResult
from agent_comms.cli_protocol import EarlierItems, Outcome, DeliveredOutcome
from .history import CodexHistory, HarnessAddress, OnlineHistory
from .shared import Binding, IncomingFrame, TranscriptBridge, Options, run_bridge
from agent_comms.runtime_updates import (
    EarlierPublishedPage, OfferedToolsObserved, PublishedPage, RuntimeUpdate, SessionFacts, ThreadOutput,
)
from agent_comms.thread_identity import UnreportedTools
from .codex_protocol import (
    Capabilities, ClientInfo, DisplayOutput, Initialize, InitializeResponse, Initialized, MessageInput,
    Request, RPC_RESPONSE, RpcReply, RpcNotification, TextInput,
    ToolOutput, RequestParams, ResumeThread, ResumedThread, ItemsList, ItemsPage, ItemStream, BusInput,
)


class CodexConnection:
    """One WebSocket owns JSON-RPC initialization and response matching."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self.socket = socket
        self.sequence = 0
        self.pending: deque[RpcNotification] = deque()
        self.answers: dict[int, asyncio.Future[JsonValue]] = {}
        self.initialized: InitializeResponse

    def answer(self, request_id: int | None) -> asyncio.Future[JsonValue]:
        if request_id is None:
            raise ConnectionError('Codex reported a failure outside any request')
        try:
            return self.answers.pop(request_id)
        except KeyError as error:
            raise ValueError('Codex replied to an unknown request') from error

    async def receive(self) -> RpcReply:
        frame = await self.socket.receive()
        if frame.type is not aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f'Codex connection ended: {frame.type.name}')
        return RPC_RESPONSE.validate_json(frame.data)

    async def call(self, params: RequestParams) -> JsonValue:
        self.sequence += 1
        request = Request[type(params)](id=self.sequence, method=params.method, params=params)
        future = asyncio.get_running_loop().create_future()
        self.answers[request.id] = future
        try:
            async with asyncio.timeout(15):
                await self.socket.send_str(request.model_dump_json(by_alias=True))
                while not future.done():
                    (await self.receive()).route(self)
                return await future
        finally:
            self.answers.pop(request.id, None)
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

@dataclass
class CodexOptions(Options):
    socket: str


class CodexAppServer(CodexHistory, OnlineHistory):
    """A thread the running Codex app-server can resume, read through its item list."""

    SOCKET = Path.home() / '.codex/app-server-control/app-server-control.sock'

    def __init__(self, thread: str, socket: str) -> None:
        self.thread = thread
        self.socket = socket

    @classmethod
    def connected(cls, thread: str, address: HarnessAddress, directory: Path) -> Self:
        return cls(thread, address.otherwise(str(cls.SOCKET)))

    async def history(self) -> AsyncIterator[RuntimeUpdate]:
        async with CodexConnection.open(self.socket) as codex:
            async for update in self.seen(codex):
                yield update

    async def entries(self, codex: CodexConnection) -> PublishedPage:
        """Only the newest page; the pager requests an earlier page when it reaches it."""
        return ItemsPage.model_validate(await codex.call(ItemsList(threadId=self.thread))).publication()

    async def seen(self, codex: CodexConnection) -> AsyncIterator[RuntimeUpdate]:
        resumed = ResumedThread.model_validate(await codex.call(ResumeThread(threadId=self.thread)))
        yield SessionFacts(source_id=self.thread, project=resumed.thread.cwd)
        yield OfferedToolsObserved(UnreportedTools(codex.initialized.userAgent))
        # Only completed snapshots. An active item's deltas must be assembled
        # from its actual start, not appended to an overlapping snapshot
        # fetched concurrently with those deltas.
        yield await self.entries(codex)
        # Codex's own thread status says whether its newest turn still runs.
        yield resumed.thread.status.update()


class CodexBridge(CodexAppServer, TranscriptBridge[CodexOptions], declared_name=None):

    def __init__(self, options: CodexOptions, binding: Binding, db: sqlite3.Connection) -> None:
        TranscriptBridge.__init__(self, options, binding, db)
        CodexAppServer.__init__(self, binding.thread, options.socket)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--socket', default=str(cls.SOCKET))

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        async with CodexConnection.open(self.socket) as codex:
            async for update in self.seen(codex):
                yield update
            stream = ItemStream(self.thread)
            async for notification in codex.notifications():
                for update in stream.receive(notification):
                    yield update

    async def earlier(self, request: EarlierItems) -> None:
        transcript = await asyncio.to_thread(self.comms.transcripts.external, self.binding.bus)
        def pending():
            with transcript.reading() as records:
                return records.pending(request)
        if not await asyncio.to_thread(pending):
            return
        async with CodexConnection.open(self.socket) as codex:
            page = ItemsPage.model_validate(await codex.call(ItemsList(
                threadId=self.thread, cursor=request.request.cursor))).publication()
        await asyncio.to_thread(EarlierPublishedPage(page.items, page.earlier, request).publish,
                                ThreadOutput(transcript, self.comms.agents))

    async def deliver(self, message: Message) -> type[Outcome]:
        async with CodexConnection.open(self.socket) as codex:
            await codex.call(BusInput.for_message(self.thread, message.message_id, IncomingFrame(message).text))
        return DeliveredOutcome

    async def show_sent(self, result: SendResult) -> None:
        async with CodexConnection.open(self.socket) as codex:
            await codex.call(DisplayOutput(
                threadId=self.thread, clientUserMessageId=f'comms-out:{result.message.message_id}',
                toolOutput=ToolOutput(output=self.sent_text(result))))

    async def report_failure(self, text: str) -> None:
        async with CodexConnection.open(self.socket) as codex:
            await codex.call(MessageInput(
                threadId=self.thread, clientUserMessageId=f'bridge-error:{uuid4()}',
                input=(TextInput(text=text),)))


def main() -> None:
    run_bridge(CodexBridge, CodexOptions.parse(__doc__, Path.home() / '.local/state/codex-comms-inbox', CodexBridge))

