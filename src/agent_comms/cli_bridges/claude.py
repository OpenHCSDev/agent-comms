"""Claude Code sessions: their stored transcripts, and the live bridge that follows one."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, BinaryIO, ClassVar

from agent_comms.bus import Bus
from agent_comms.harness_delivery_states import Outcome
from agent_comms.messages import Message
from agent_comms.private_path import FileRevision
from agent_comms.runtime_updates import RuntimeUpdate
from agent_comms.watched import FileSource
from agent_comms_hooks.session import SessionHook
from agent_comms_hooks.watch import WatchClaim

from .history import ClaudeHistory
from .claude_protocol import ClaudeMonitorFrame
from .claude_transcript import ClaudeJsonLines, RunningClaude
from .shared import Enrol, LineMonitorBridge, Options, Watch, run_bridge

if TYPE_CHECKING:
    from agent_comms.task_attachments.commander_pings import CommanderUpdate


class ClaudeBridge(ClaudeJsonLines, ClaudeHistory, LineMonitorBridge):
    """Follow this Claude session's file without submitting an input.

    Heartbeat packets reach the session through the board its tool-call hook prints, which
    this bridge derives from Core whenever the hook asks, not through the Monitor: the harness
    frames Monitor events as routine background, so a line said once there scrolls away. The
    Monitor only gets a wake line, since an idle session runs no tool calls."""

    board_wake: ClassVar[str] = (
        "Tristan: your board changed. New and changed lines print once as BOARD: on your next "
        "tool call; later calls print one unchanged count."
    )

    incoming_frame = ClaudeMonitorFrame
    window_bytes = ClaudeMonitorFrame.BATCH_CHARS * 2
    RECENT_BYTES = 4 * 1024 * 1024
    state_directory = Path('.local/state/claude-comms-inbox')

    @classmethod
    def environment_session(cls) -> str:
        return SessionHook.environment_session()

    async def watch_operation(self, operation: Watch) -> None:
        state = WatchClaim(self.address.thread).state()
        await asyncio.to_thread(state.rearm, partial(Connect().begin, self))
        await super().watch_operation(operation)

    async def deliver_board(self, update: CommanderUpdate, message: Message) -> type[Outcome]:
        """The packet is on the bus, which is the board; only the session needs waking."""
        return await update.wake(self, message)

    async def wake(self, message: Message) -> type[Outcome]:
        return await self.deliver(replace(message, body=self.board_wake))

    def history_start(self, stream: BinaryIO, boundary: int) -> int:
        """Retain the recent window's owning occurrence, never an arbitrary mid-turn prefix."""
        start = max(0, boundary - self.RECENT_BYTES)
        if start:
            stream.seek(start - 1)
            stream.readline()
            start = stream.tell()
        return self.opening_before(stream, start)

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        session = self.start_state()
        source = self.options.operation.transcript_source()
        async with aclosing(Bus.watch_sources(FileSource(source))) as changes:
            await anext(changes)
            while not source.exists():
                await anext(changes)
            with source.open('rb') as stream:
                original = FileRevision.from_file(stream)
                for update in self.decoded(stream, self.history_start(stream, original.size), original.size, session):
                    yield update
                # Only the current status is published from the recent window.
                for update in session.phase.window_ended(session):
                    yield update
                async for _ in changes:
                    opened = FileRevision.from_file(stream)
                    replaced = ValueError('Claude transcript was replaced or truncated')
                    current = FileRevision.capture(source).require(replaced)
                    if (current.identity != original.identity
                            or min(opened.size, current.size) < stream.tell()):
                        raise replaced
                    for update in self.decoded(stream, stream.tell(), opened.size, session):
                        yield update


class Connect(Enrol):
    help = "Bind this running Claude session to a bus thread and start its bridge now"
    reported = 'Session bridge running and ready. Run watch under Monitor to receive.'

    @classmethod
    def serves(cls, bridge: type) -> bool:
        return issubclass(bridge, ClaudeBridge)

    def begin(self, bridge: LineMonitorBridge) -> None:
        """Serve.launch raises unless the started bridge answers its readiness exchange."""
        SessionHook.current(
            bridge.address.thread, str(RunningClaude(bridge.address.thread).source)
        ).start()


def main() -> None:
    run_bridge(ClaudeBridge, Options.parse(
        __doc__, ClaudeBridge.state_path(), ClaudeBridge))
