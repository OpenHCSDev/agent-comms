"""Claude Code sessions: their stored transcripts, and the live bridge that follows one."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from collections.abc import Iterator
from contextlib import aclosing
from functools import cached_property
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Self

from agent_comms.child_process import AttachedChild, ProcessIdentity, StreamingChildStdio
from agent_comms.cli_protocol import DeliveredOutcome, Outcome, WireModel, raise_child_exit
from agent_comms.messages import Message
from agent_comms.registration import Registration
from agent_comms.coordination_database import CoordinationStore
from agent_comms.private_path import FileRevision
from agent_comms.wire_watch import WatchSource, WireWatch
from .claude_protocol import CLAUDE_RECORD, ClaudeRecord, ClaudeSession, ClaudeMonitorFrame, ClaudeInputFrame
from .history import ClaudeHistory, JsonLinesFile, OfflineHistory, OnlineHistory
from .shared import Binding, CliBridge, LineMonitorBridge, Operation, Options, run_bridge
from agent_comms.runtime_updates import RuntimeUpdate, SessionFacts


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
        for update in record.updates(state):
            yield from update.during(state.phase)

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        """The file is named by its session; a subagent's task description is beside it, not in it."""
        meta = self.source.with_suffix('.meta.json')
        title = SubagentMeta.model_validate_json(meta.read_bytes()).description if meta.is_file() else ''
        yield SessionFacts(source_id=self.source.stem, title=title)


class ClaudeTranscript(ClaudeJsonLines, ClaudeHistory, OfflineHistory):
    """Claude Code writes one transcript file per session (`<id>.jsonl`) and per subagent
    (`<id>/subagents/agent-<id>.jsonl`), under ~/.claude/projects."""

    suffixes = frozenset({'.jsonl'})

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        if self.session_id not in (None, self.source.stem):
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
    def connected(cls, thread: str, address: str | None, directory: Path) -> Self:
        return cls(thread)


class ClaudeBridge(RunningClaude, LineMonitorBridge, declared_name=None):
    """Follow this Claude session's file without submitting an input."""

    incoming_frame = ClaudeMonitorFrame
    window_bytes = ClaudeMonitorFrame.BATCH_CHARS * 2
    RECENT_BYTES = 4 * 1024 * 1024

    def __init__(self, options: Options, binding: Binding, db: CoordinationStore) -> None:
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
        with self.source.open('rb') as stream:
            original = FileRevision.from_file(stream)
            async with aclosing(WireWatch.observations(WatchSource.modified_file(self.source))) as changes:
                for update in self.decoded(stream, self.history_start(stream, original.size), original.size, session):
                    yield update
                # Only the current status is published from the recent window.
                for update in session.phase.window_ended(session):
                    yield update
                async for _ in changes:
                    opened = FileRevision.from_file(stream)
                    current = FileRevision.capture(self.source)
                    if (current is None or current.identity != original.identity
                            or min(opened.size, current.size) < stream.tell()):
                        raise ValueError('Claude transcript was replaced or truncated')
                    for update in self.decoded(stream, stream.tell(), opened.size, session):
                        yield update


class ClaudeInputBridge(ClaudeBridge, declared_name=None):
    """Own Claude's actual stdin; Monitor remains for externally started interactive sessions."""

    def __init__(self, options: Options, binding: Binding, db: CoordinationStore) -> None:
        super().__init__(options, binding, db)
        self.pending: dict[str, asyncio.Future[None]] = {}
        self.transcript_ready = asyncio.Event()

    async def prepare_delivery(self) -> None:
        """The registered child owns this input pipe; no Monitor attachment is needed."""

    async def send_input(self, identity: str, payload: bytes) -> None:
        receipt = asyncio.get_running_loop().create_future()
        self.pending[identity] = receipt
        try:
            await self.child.write(payload)
            await receipt  # Claude's own replay, not pipe flush, confirms acceptance.
        finally:
            self.pending.pop(identity, None)

    async def deliver(self, message: Message) -> type[Outcome]:
        await self.input.turn_start(message)
        return DeliveredOutcome

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        # A fresh session has no transcript until Claude writes its first answer.
        await self.transcript_ready.wait()
        async for update in super().transcript():
            yield update

    async def receive(self) -> None:
        assert self.child.stdout is not None
        while line := await self.child.stdout.readline():
            event = json.loads(line)
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            if event['type'] == 'user':
                receipt = self.pending.get(event['uuid'])
                if receipt is not None and not receipt.done():
                    receipt.set_result(None)
            elif event['type'] == 'assistant':
                self.transcript_ready.set()
        await raise_child_exit(self.child, '')
        raise EOFError('Claude input session exited; no pending input was retried')


@dataclass(frozen=True)
class Run(Operation[ClaudeBridge]):
    """Start a bus-addressable Claude session without requiring an initial user prompt."""

    claude: str
    arguments: tuple[str, ...]

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, ClaudeBridge)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--claude', default='claude')
        parser.add_argument('--claude-arg', dest='arguments', action='append', default=[])

    async def run(self, bridge: ClaudeBridge) -> None:
        session = ClaudeInputBridge(bridge.options, bridge.binding, bridge.db)
        session.child = await AttachedChild.start(
            ClaudeInputFrame.command(self.claude, bridge.binding.thread, self.arguments),
            stdio=StreamingChildStdio(limit=8 * 1024 * 1024, stderr=sys.stderr))
        try:
            registry = session.comms.registry
            with registry.external_input(session.binding.bus, ProcessIdentity.capture(os.getpid()),
                                         ClaudeInputFrame, session.send_input) as session.input:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(session.receive())
                    tasks.create_task(session.publish_transcript())
                    tasks.create_task(session.watch())
        finally:
            await session.child.stop()


def main() -> None:
    run_bridge(ClaudeBridge, Options.parse(__doc__, Path.home() / '.local/state/claude-comms-inbox', ClaudeBridge))
