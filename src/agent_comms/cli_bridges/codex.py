"""Codex sessions held by a running app-server, and the bridge that follows one."""
from __future__ import annotations

import argparse
import asyncio
import socket
import sqlite3
import struct
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import AsyncIterator, Self

from agent_comms.child_process import ProcessIdentity
from agent_comms.messages import Message
from agent_comms.send_result import SendResult
from agent_comms.cli_protocol import EarlierItems, Outcome, DeliveredOutcome
from .history import CodexHistory, HarnessAddress, OnlineHistory
from .shared import SessionBinding, IncomingFrame, TranscriptBridge, Options, run_bridge
from agent_comms.runtime_updates import (
    EarlierPublishedPage, OfferedToolsObserved, PublishedPage, RuntimeUpdate, SessionFacts, ThreadOutput,
)
from agent_comms.thread_identity import UnreportedTools
from .codex_connection import CodexConnection
from .codex_protocol import (
    DisplayOutput, ToolOutput, ResumeThread, ResumedThread, ItemsList, ItemsPage, ItemStream, BusInput,
)


@dataclass
class CodexOptions(Options):
    socket: str


class CodexAppServer(CodexHistory, OnlineHistory):
    """A thread the running Codex app-server can resume, read through its item list."""

    def __init__(self, thread: str, socket: str) -> None:
        self.thread = thread
        self.socket = socket

    @classmethod
    def connected(cls, thread: str, address: HarnessAddress, directory: Path) -> Self:
        return cls(thread, address.otherwise(str(CodexConnection.CONTROL_SOCKET)))

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


class CodexBridge(CodexAppServer, TranscriptBridge[CodexOptions]):
    state_directory = Path('.local/state/codex-comms-inbox')

    def __init__(self, options: CodexOptions, address: SessionBinding, db: sqlite3.Connection) -> None:
        TranscriptBridge.__init__(self, options, address, db)
        CodexAppServer.__init__(self, address.thread, options.socket)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--socket', default=str(CodexConnection.CONTROL_SOCKET))

    def session_process(self) -> ProcessIdentity:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.connect(self.socket)
            pid, _uid, _gid = struct.unpack(
                '3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        return ProcessIdentity.capture(pid)

    async def session_worktree(self) -> Path:
        async with CodexConnection.open(self.socket) as codex:
            resumed = ResumedThread.model_validate(await codex.call(
                ResumeThread(threadId=self.thread)))
            return Path(resumed.thread.cwd)

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
        await self.comms.bus.when_open(partial(EarlierPublishedPage(page.items, page.earlier, request).publish,
                                               ThreadOutput(transcript, self.comms.agents)))

    async def deliver(self, message: Message) -> type[Outcome]:
        async with CodexConnection.open(self.socket) as codex:
            await codex.call(BusInput.for_message(self.thread, message.message_id, IncomingFrame(message).text))
        return DeliveredOutcome

    async def show_sent(self, result: SendResult) -> None:
        async with CodexConnection.open(self.socket) as codex:
            await codex.call(ResumeThread(threadId=self.thread))
            await codex.call(DisplayOutput(
                threadId=self.thread, clientUserMessageId=f'comms-out:{result.message.message_id}',
                toolOutput=ToolOutput(output=self.sent_text(result))))


def main() -> None:
    run_bridge(CodexBridge, CodexOptions.parse(
        __doc__, CodexBridge.state_path(), CodexBridge))

