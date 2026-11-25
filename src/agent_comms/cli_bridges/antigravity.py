"""Antigravity sessions: their stored transcripts, and the live bridge that follows one."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

from agent_comms.cli_bridges.antigravity_transcript import (
    RecentAntigravityWindow,
    RunningAntigravity,
)
from agent_comms.cli_bridges.claude_protocol import ClaudeMonitorFrame
from agent_comms.cli_bridges.goal_semantics import AntigravityGoalHarness
from agent_comms.cli_bridges.history import AntigravityHistory
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate
from agent_comms.cli_bridges.shared import LineMonitorBridge, Options, run_bridge
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.watched import FileSource
from agent_comms.storage.file_observation import FileRevision


class AntigravityBridge(RecentAntigravityWindow, AntigravityHistory, LineMonitorBridge):
    """Follow an Antigravity conversation's `transcript.jsonl` and deliver bus messages
    through the shared `LineMonitorBridge` delivery journal."""

    incoming_frame = ClaudeMonitorFrame
    window_bytes = ClaudeMonitorFrame.BATCH_CHARS * 2
    state_directory = Path(".local/state/antigravity-comms-inbox")
    goal_harness = AntigravityGoalHarness

    @classmethod
    def environment_session(cls) -> str:
        active = RunningAntigravity.active()
        return active.conversation_id_of(active.source)

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        session = self.start_state()
        source = RunningAntigravity(self.address.thread).source
        async with aclosing(Bus.watch_sources(FileSource(source))) as changes:
            await anext(changes)
            while not source.exists():
                await anext(changes)
            with source.open("rb") as stream:
                original = FileRevision.from_file(stream)
                for update in self.decoded(
                    stream, self.history_start(stream, original.size), original.size, session
                ):
                    yield update
                for update in session.phase.window_ended(session):
                    yield update
                async for _ in changes:
                    opened = FileRevision.from_file(stream)
                    replaced = ValueError("Antigravity transcript was replaced or truncated")
                    current = FileRevision.capture(source).require(replaced)
                    if (
                        current.identity != original.identity
                        or min(opened.size, current.size) < stream.tell()
                    ):
                        raise replaced
                    for update in self.decoded(stream, stream.tell(), opened.size, session):
                        yield update


def main() -> None:
    run_bridge(
        AntigravityBridge,
        Options.parse(__doc__, AntigravityBridge.state_path(), AntigravityBridge),
    )
