"""Claude Code sessions: their stored transcripts, and the live bridge that follows one."""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import aclosing
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Self, cast

from agent_comms.cli_protocol import WireModel
from agent_comms.coordination_database import CoordinationStore
from agent_comms.private_path import FileRevision
from agent_comms.importing import ImportSnapshot
from agent_comms.wire_watch import WatchSource, WireWatch
from .claude_protocol import CLAUDE_RECORD, ClaudeRecord, ClaudeSession, ClaudeMonitorFrame
from .history import ClaudeHistory, JsonLinesFile, OfflineHistory, OnlineHistory
from .shared import Binding, LineMonitorBridge, Options, run_bridge
from agent_comms.runtime_updates import RuntimeUpdate, SessionFacts


class SubagentMeta(WireModel):
    description: str


@dataclass(frozen=True)
class SubagentInfo:
    id: str
    description: str | None
    message_count: int
    source_records: int


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

    @property
    def launch_description(self) -> str | None:
        """Claude saves launch descriptions beside the JSONL, not in its first record."""
        meta = self.source.with_suffix('.meta.json')
        return SubagentMeta.model_validate_json(meta.read_bytes()).description if meta.is_file() else None

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        yield SessionFacts(source_id=self.source.stem, title=self.launch_description or '')


class ClaudeTranscript(ClaudeJsonLines, ClaudeHistory, OfflineHistory):
    """Claude Code writes one transcript file per session (`<id>.jsonl`) and per subagent
    (`<id>/subagents/agent-<id>.jsonl`), under ~/.claude/projects."""

    suffixes = frozenset({'.jsonl'})

    ancestors: tuple[Path, ...] = ()

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        if self.session_id not in (None, self.source.stem):
            raise ValueError('The transcript belongs to a different Claude session.')
        return super().facts(stream, boundary)

    def import_updates(self, record: object, state: object,
                       offset: int, raw: bytes) -> Iterator[RuntimeUpdate]:
        yield from cast(ClaudeRecord, record).import_updates(self, cast(ClaudeSession, state), offset, raw)

    def inherited_updates(self, session_id: str, last_uuid: str) -> Iterator[RuntimeUpdate]:
        directory = self.source.parent
        if directory.name == 'subagents':
            directory = directory.parent.parent
        if Path(session_id).name != session_id:
            raise ValueError(f'Invalid inherited Claude session id: {session_id}')
        parent = ClaudeTranscript(directory / f'{session_id}.jsonl')
        parent.ancestors = (*self.ancestors, self.source)
        if parent.source in parent.ancestors:
            raise ValueError(f'Cyclic inherited Claude context: {parent.source}')
        state = parent.start_state()
        with parent.source.open('rb') as stream:
            for offset, raw in parent.import_records(stream, stream.seek(0, 2)):
                record = parent.decode_line(raw, offset)
                for update in parent.import_updates(record, state, offset, raw):
                    # Inherit conversation, never the parent's identity, project or title.
                    if not isinstance(update, SessionFacts):
                        yield update
                if json.loads(raw).get('uuid') == last_uuid:
                    return
        raise ValueError(f'Inherited Claude context {session_id} has no record {last_uuid}')

    def subagent_info(self) -> SubagentInfo:
        snapshot = ImportSnapshot.read(self)
        with self.source.open('rb') as stream:
            source_records = sum(1 for _ in self.import_records(stream, stream.seek(0, 2)))
        return SubagentInfo(self.source.stem.removeprefix('agent-'), self.launch_description,
                            len(snapshot.messages), source_records)

    @classmethod
    def select_subagents(cls, session: Path, agents: tuple[str, ...]) -> tuple[Path, ...]:
        paths = cls.subagents(session)
        available = {path.stem.removeprefix('agent-'): path for path in paths}
        missing = set(agents) - available.keys()
        if missing:
            raise ValueError('Unknown subagent id(s): ' + ', '.join(sorted(missing)))
        return tuple(available[agent] for agent in dict.fromkeys(agents)) if agents else paths

    @staticmethod
    def subagents(session: Path) -> tuple[Path, ...]:
        """The subagent transcripts a stored session started, oldest first."""
        if not session.is_file():
            raise ValueError(f'Claude session does not exist: {session}')
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


def main() -> None:
    run_bridge(ClaudeBridge, Options.parse(__doc__, Path.home() / '.local/state/claude-comms-inbox', ClaudeBridge))
