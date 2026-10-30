"""Claude Code sessions: their stored transcripts, and the live bridge that follows one."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import AsyncIterator, BinaryIO

from agent_comms.bus import Bus
from agent_comms.private_path import FileRevision
from agent_comms.runtime_updates import RuntimeUpdate
from agent_comms.watched import FileSource
from agent_comms_hooks.bridged import BridgedToolCall
from agent_comms_hooks.reread import ToolHookEvent
from agent_comms_hooks.session import SessionHook
from agent_comms_hooks.watch import WatchClaim

from .history import ClaudeHistory
from .claude_protocol import ClaudeMonitorFrame
from .claude_transcript import ClaudeJsonLines, RunningClaude
from .monitor import MonitorPeer, MonitorService, SessionRefused, SessionReply, SessionRequest
from .shared import Enrol, LineMonitorBridge, Options, Watch, run_bridge

class ClaudeBridge(ClaudeJsonLines, ClaudeHistory, LineMonitorBridge):
    """Follow the Claude session and deliver the original waking HUD through its native frame.

    Packet-owned wake behavior is shared with every bridge; tool-call board reads still derive
    their current undisposed requests from Core rather than keeping another copy here.
    The native frame receives the original message: rewriting its body changes the identity
    the delivery attempt was stored under.
    """

    incoming_frame = ClaudeMonitorFrame
    window_bytes = ClaudeMonitorFrame.BATCH_CHARS * 2
    RECENT_BYTES = 4 * 1024 * 1024
    state_directory = Path('.local/state/claude-comms-inbox')

    @classmethod
    def environment_session(cls) -> str:
        return WatchClaim.environment_session()

    async def watch_operation(self, operation: Watch) -> None:
        state = WatchClaim(self.address.thread).state()
        await asyncio.to_thread(state.rearm, partial(Connect().begin, self))
        await super().watch_operation(operation)

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


@dataclass(frozen=True)
class HookAnswer(SessionReply, declared_name=BridgedToolCall.request):
    """What a tool-call hook prints, decided here; its client decodes it with the standard
    library."""

    output: str


@dataclass(frozen=True)
class ToolCallHook(SessionRequest[HookAnswer], declared_name=BridgedToolCall.request):
    """A tool-call hook of this session (the read gate, the reminder) asking this bridge, which
    holds Core open, to decide Claude Code's record, so no hook process imports Core."""

    record: str

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        """A hook's failure was its own process's exit; here it is that hook's refusal, never
        an exception that would end the bridge serving every other call."""
        try:
            return HookAnswer(await asyncio.to_thread(ToolHookEvent.answered, self.record))
        except (Exception, SystemExit) as error:
            return SessionRefused(error=f"{type(error).__name__}: {error}")


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
