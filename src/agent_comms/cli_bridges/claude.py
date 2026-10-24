"""Claude Code sessions: their stored transcripts, and the live bridge that follows one."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterator
from contextlib import aclosing
from functools import cached_property
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Self

from agent_comms.bus import Bus
from agent_comms.private_path import FileRevision
from agent_comms.runtime_updates import RuntimeUpdate, SessionFacts
from agent_comms.thread_text import NoThreadText, ThreadText
from agent_comms.watched import FileSource
from agent_comms_hooks.session import SessionHook
from agent_comms_hooks.watch import WatchClaim

from ..wire_models import WireModel
from .claude_protocol import CLAUDE_RECORD, ClaudeMonitorFrame, ClaudeRecord, ClaudeSession
from .history import ClaudeHistory, HarnessAddress, JsonLinesFile, OfflineHistory, OnlineHistory
from .shared import Binding, Enrol, LineMonitorBridge, Options, run_bridge


class SubagentMeta(WireModel):
    description: str


class ClaudeJsonLines(JsonLinesFile):
    """How a Claude Code transcript file decodes, whether stored or still being written."""

    records = CLAUDE_RECORD
    state = ClaudeSession

    def decode_line(self, raw: bytes, position: int):
        return self.records.validate_python({**json.loads(raw), 'position': position})

    def record_updates(self, record: ClaudeRecord, state: ClaudeSession, offset: int,
                       raw: bytes) -> Iterator[RuntimeUpdate]:
        yield from state.located(record.cwd)
        for update in (*state.version_reported(record.version), *record.updates(state)):
            yield from update.during(state.phase)

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        """The file is named by its session; a subagent's task description is beside it, not in it."""
        meta = self.source.with_suffix('.meta.json')
        title = (ThreadText.external(SubagentMeta.model_validate_json(meta.read_bytes()).description)
                 if meta.is_file() else NoThreadText())
        yield SessionFacts(source_id=self.source.stem, title=title)


class ClaudeTranscript(ClaudeJsonLines, ClaudeHistory, OfflineHistory):
    """Claude Code writes one transcript file per session (`<id>.jsonl`) and per subagent
    (`<id>/subagents/agent-<id>.jsonl`), under ~/.claude/projects."""

    suffixes = frozenset({'.jsonl'})

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        if not self.session.holds(self.source.stem):
            raise ValueError('The transcript belongs to a different Claude session.')
        return super().facts(stream, boundary)

    @staticmethod
    def subagents(session: Path) -> tuple[Path, ...]:
        """The subagent transcripts a stored session started, oldest first."""
        return tuple(sorted(session.with_suffix('').glob('subagents/agent-*.jsonl'),
                            key=lambda path: path.stat().st_mtime))


class RunningClaude(ClaudeJsonLines, ClaudeHistory, OnlineHistory):
    """A session Claude Code is writing, found by its session id. Claude has no server to ask,
    so the running session is read from the same file as a stored one."""

    def __init__(self, thread: str) -> None:
        self.thread = thread

    @cached_property
    def source(self) -> Path:
        matches = tuple((Path.home() / '.claude/projects').glob(f'*/{self.thread}.jsonl'))
        if len(matches) != 1:
            raise ValueError(f'Expected one Claude transcript for {self.thread}; found {len(matches)}')
        return matches[0]

    @classmethod
    def connected(cls, thread: str, address: HarnessAddress, directory: Path) -> Self:
        return cls(thread)


class ClaudeBridge(RunningClaude, LineMonitorBridge, declared_name=None):
    """Follow this Claude session's file without submitting an input."""

    incoming_frame = ClaudeMonitorFrame
    window_bytes = ClaudeMonitorFrame.BATCH_CHARS * 2
    RECENT_BYTES = 4 * 1024 * 1024

    @classmethod
    def binding_arguments(cls, arguments: argparse.Namespace) -> None:
        """A bare Monitor command uses the binding enrolled for this Claude session."""
        if arguments.bus_thread is not None:
            return
        session = arguments.session or SessionHook.environment_session()
        bindings = WatchClaim(session).state().bindings()
        if len(bindings) != 1:
            raise ValueError(
                f'Expected one enrolled binding for session {session}; '
                f'found {[record.bus for record in bindings]}'
            )
        record, = bindings
        arguments.root = record.root
        arguments.bus_thread = record.bus
        arguments.state = record.state
        arguments.session = session

    def __init__(self, options: Options, binding: Binding, db: sqlite3.Connection) -> None:
        LineMonitorBridge.__init__(self, options, binding, db)
        RunningClaude.__init__(self, binding.thread)

    def history_start(self, stream: BinaryIO, boundary: int) -> int:
        """On restart only the last 4 MiB is published again; Core's finish is idempotent, so
        completed items may overlap. The first record starts after a newline in that window."""
        start = max(0, boundary - self.RECENT_BYTES)
        if start:
            stream.seek(start - 1)
            stream.readline()
            start = stream.tell()
        return start

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
            bridge.binding.thread, str(RunningClaude(bridge.binding.thread).source)
        ).start()


def main() -> None:
    run_bridge(ClaudeBridge, Options.parse(__doc__, Path.home() / '.local/state/claude-comms-inbox', ClaudeBridge))
