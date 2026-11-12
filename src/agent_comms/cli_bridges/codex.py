"""Codex sessions held by a running app-server, and the bridge that follows one."""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sqlite3
import struct
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import AsyncIterator, ClassVar, Self

import aiohttp

from agent_comms.cli_bridges.cli_protocol import EarlierItems
from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.cli_bridges.codex_protocol import (
    BusInput,
    CodexDraining,
    DisplayOutput,
    ItemsList,
    ItemsPage,
    ItemStream,
    ResumedThread,
    ResumeThread,
    ToolOutput,
)
from agent_comms.cli_bridges.harness_delivery_states import AcceptedOutcome, Outcome
from agent_comms.cli_bridges.history import CodexHistory, HarnessAddress, OnlineHistory
from agent_comms.cli_bridges.runtime_updates import (
    EarlierPublishedPage,
    OfferedToolsObserved,
    PublishedPage,
    RuntimeUpdate,
    SessionFacts,
    ThreadOutput,
)
from agent_comms.cli_bridges.session_watch import WatchClaim, WatchRecord
from agent_comms.cli_bridges.shared import (
    CliBridge,
    IncomingFrame,
    Operation,
    Options,
    SessionBinding,
    SuppliedSession,
    TranscriptBridge,
    Watch,
    run_bridge,
)
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.host_services.codex_inbox import CodexInboxService
from agent_comms.messaging.messages import Message
from agent_comms.messaging.send_result import SendResult
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import UnreportedTools


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
    reconnect_seconds: ClassVar[float] = 1.0

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

    async def session_worktree(self) -> Checkout:
        async with CodexConnection.open(self.socket) as codex:
            resumed = ResumedThread.model_validate(await codex.call(
                ResumeThread(threadId=self.thread)))
            return Checkout.from_wire(resumed.thread.cwd)

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        async with CodexConnection.open(self.socket) as codex:
            async for update in self.seen(codex):
                yield update
            stream = ItemStream(self.thread)
            async for notification in codex.notifications():
                if (update := stream.receive(notification)) is not None:
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
        request = BusInput.for_message(self.thread, message.message_id, IncomingFrame(message).text)
        retries = 0
        while True:
            try:
                async with CodexConnection.open(self.socket) as codex:
                    await codex.call(request)
            except (CodexDraining, aiohttp.ClientConnectorError) as absent:
                # Only these two prove no turn started: the connection never opened, or the server
                # refused while draining and asks for the same turn after reconnecting.
                if not retries:
                    self.log(f'Codex unavailable ({absent}); sending {message.message_id} once it is back')
                retries += 1
                await asyncio.sleep(self.reconnect_seconds)
                continue
            if retries:
                self.log(f'Codex is back; sent {message.message_id} after {retries} refused attempts')
            return AcceptedOutcome

    async def show_sent(self, result: SendResult) -> None:
        async with CodexConnection.open(self.socket) as codex:
            await codex.call(ResumeThread(threadId=self.thread))
            await codex.call(DisplayOutput(
                threadId=self.thread, clientUserMessageId=f'comms-out:{result.message.message_id}',
                toolOutput=ToolOutput(output=self.sent_text(result))))


@dataclass(frozen=True)
class Connect(Watch):
    help = "Connect this Codex conversation and start its live inbox, replies and status"

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, CodexBridge)

    async def register(self, bridge: CodexBridge) -> None:
        await super().register(bridge)
        # A saved native transcript is not a live harness endpoint. Refuse before launch.
        await bridge.comms.bus.when_open(partial(
            bridge.comms.transcripts.external, bridge.address.bus))
        await bridge.session_worktree()

    async def execute(self, bridge: CodexBridge) -> None:
        command = (
            CodexInboxService.program,
            Watch.command_name(),
            '--state', str(bridge.options.base_state.absolute()),
            '--socket', str(Path(bridge.options.socket).absolute()),
            '--root', str(bridge.comms.root),
            '--bus-thread', bridge.binding.bus,
            '--session', bridge.binding.thread,
        )
        record = WatchRecord(bridge.binding.bus, str(bridge.comms.root), command,
                             bridge.options.base_state.absolute())
        await asyncio.to_thread(WatchClaim(bridge.binding.thread).enrol, record)
        service = CodexInboxService(bridge.binding.thread)
        await asyncio.to_thread(service.connect)
        print(f'Connected {bridge.binding.bus}; live inbox, replies and status started.',
              flush=True)


@dataclass(frozen=True)
class Resume(Operation[CodexBridge]):
    help = "Start the exact live Codex connection saved by connect"
    binding_session = SuppliedSession

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, CodexBridge)

    async def execute(self, bridge: CodexBridge) -> None:
        record = WatchClaim(bridge.binding.thread).recorded()
        if Path(record.command[0]).name != CodexInboxService.program:
            raise ValueError('This session is enrolled with a different harness')
        executable = LaunchRuntime.installed().interpreter.with_name(CodexInboxService.program)
        os.execv(executable, (str(executable), *record.command[1:]))


def main() -> None:
    run_bridge(CodexBridge, CodexOptions.parse(
        __doc__, CodexBridge.state_path(), CodexBridge))

