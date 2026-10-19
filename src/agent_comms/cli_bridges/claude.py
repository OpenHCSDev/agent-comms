"""Claude Code sessions: their stored transcripts, and the live bridge that follows one."""
from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from collections.abc import Iterator
from contextlib import aclosing
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Self

from agent_comms.child_process import AttachedChild, StreamingChildStdio
from agent_comms.cli_protocol import WireModel, raise_child_exit
from agent_comms.coordination_database import CoordinationStore
from agent_comms.private_path import FileRevision
from agent_comms.wire_watch import WatchSource, WireWatch
from agent_comms_hooks.watch import WatchClaim
from .claude_protocol import CLAUDE_RECORD, ClaudeRecord, ClaudeSession, ClaudeMonitorFrame, ClaudeInputFrame
from .history import ClaudeHistory, JsonLinesFile, OfflineHistory, OnlineHistory
from .monitor import AttachInput, InputReceipt, REPLIES
from .shared import Binding, CliBridge, Enrol, LineMonitorBridge, Operation, Options, run_bridge
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
        meta = self.source.with_suffix('.meta.json')
        title = SubagentMeta.model_validate_json(meta.read_bytes()).description if meta.is_file() else ''
        yield SessionFacts(source_id=self.source.stem, title=title)


class ClaudeTranscript(ClaudeJsonLines, ClaudeHistory, OfflineHistory):
    """Claude's saved session and subagent transcripts."""

    suffixes = frozenset({'.jsonl'})

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        if self.session_id not in (None, self.source.stem):
            raise ValueError('The transcript belongs to a different Claude session.')
        return super().facts(stream, boundary)

    @staticmethod
    def subagents(session: Path) -> tuple[Path, ...]:
        return tuple(sorted(session.with_suffix('').glob('subagents/agent-*.jsonl'),
                            key=lambda path: path.stat().st_mtime))


class RunningClaude(ClaudeJsonLines, ClaudeHistory, OnlineHistory):
    """Read the actual session file Claude writes, not a second history store."""

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
    """SessionStart owns the daemon; attached frontends supply only input transport."""

    incoming_frame = ClaudeMonitorFrame
    input_frame = ClaudeInputFrame
    window_bytes = ClaudeMonitorFrame.BATCH_CHARS * 2
    RECENT_BYTES = 4 * 1024 * 1024

    def __init__(self, options: Options, binding: Binding, db: CoordinationStore) -> None:
        LineMonitorBridge.__init__(self, options, binding, db)
        RunningClaude.__init__(self, binding.thread)

    @property
    def source(self) -> Path:
        return self.options.operation.transcript_source()

    def history_start(self, stream: BinaryIO, boundary: int) -> int:
        start = max(0, boundary - self.RECENT_BYTES)
        if start:
            stream.seek(start - 1)
            stream.readline()
            start = stream.tell()
        return start

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        session = self.start_state()
        async with aclosing(WireWatch.observations(WatchSource.modified_file(self.source))) as changes:
            await anext(changes)
            while not self.source.exists():
                await anext(changes)
            with self.source.open('rb') as stream:
                original = FileRevision.from_file(stream)
                for update in self.decoded(stream, self.history_start(stream, original.size), original.size, session):
                    yield update
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


class Connect(Enrol):
    @classmethod
    def serves(cls, bridge: type) -> bool:
        return issubclass(bridge, ClaudeBridge)

    async def run(self, bridge: ClaudeBridge) -> None:
        await super().run(bridge)
        from agent_comms_hooks.session import SessionHook
        SessionHook.current(bridge.binding.thread, str(RunningClaude(bridge.binding.thread).source)).start()


@dataclass(frozen=True)
class Run(Operation[ClaudeBridge]):
    """A real Claude stdin frontend; no registry, inbox watch or history of its own."""

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
        claim = WatchClaim(bridge.binding.thread)
        claim.enrol(bridge.watch_record())
        hooks = {event: [{'hooks': [{'type': 'command', 'command': shlex.join((
            sys.executable, '-c', f'from agent_comms_hooks.session import {function}; {function}()'))}]}]
            for event, function in (('SessionStart', 'started'), ('SessionEnd', 'ended'))}
        child = await AttachedChild.start(ClaudeInputFrame.command(
            self.claude, bridge.binding.thread,
            (*self.arguments, '--settings', json.dumps({'hooks': hooks}))),
            cwd=bridge.comms.registry.require(bridge.binding.bus).worktree,
            stdio=StreamingChildStdio(limit=8 * 1024 * 1024, stderr=sys.stderr))
        connected = asyncio.Event()

        async def attach() -> None:
            async with aclosing(WireWatch.observations(WatchSource.modified_file(claim.socket_path))) as changes:
                async for _ in changes:
                    if claim.socket_path.exists():
                        break
            reader, writer = await asyncio.open_unix_connection(str(claim.socket_path))
            try:
                writer.write((AttachInput(owner=child.identity).model_dump_json() + '\n').encode())
                await writer.drain()
                REPLIES.validate_json(await reader.readline()).require()
                nonlocal input_writer
                input_writer = writer
                connected.set()
                print('Managed Claude input attached to SessionStart bridge.', file=sys.stderr, flush=True)
                async for line in reader:
                    await child.write(line)
                raise ConnectionResetError('SessionStart bridge disconnected; no input retried')
            finally:
                writer.close()
                await writer.wait_closed()

        async def receive() -> None:
            assert child.stdout is not None
            while line := await child.stdout.readline():
                event = json.loads(line)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                if event['type'] == 'user':
                    await connected.wait()
                    input_writer.write((InputReceipt(identity=event['uuid']).model_dump_json() + '\n').encode())
                    await input_writer.drain()
            await raise_child_exit(child, '')
            raise EOFError('Claude session exited; no input retried')

        input_writer: asyncio.StreamWriter
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(attach())
                tasks.create_task(receive())
        finally:
            await child.stop()


def main() -> None:
    run_bridge(ClaudeBridge, Options.parse(__doc__, Path.home() / '.local/state/claude-comms-inbox', ClaudeBridge))
