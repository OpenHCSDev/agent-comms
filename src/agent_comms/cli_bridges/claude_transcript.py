"""Claude Code transcript files, read whether stored or still being written; no bridge."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Self

from agent_comms.runtime_updates import RuntimeUpdate, SessionFacts
from agent_comms.thread_text import NoThreadText, ThreadText

from ..wire_models import WireModel
from .claude_protocol import CLAUDE_RECORD, ClaudeRecord, ClaudeSession
from .history import (
    ClaudeHistory, HarnessAddress, JsonLines, JsonLinesFile, NamedSession,
    OfflineHistory, OnlineHistory,
)


class SubagentMeta(WireModel):
    description: str


class ClaudeJsonLines(JsonLines):
    """How a Claude Code transcript file decodes, whether stored or still being written."""

    records = CLAUDE_RECORD
    state = ClaudeSession

    def decode_line(self, raw: bytes, position: int) -> ClaudeRecord:
        return self.records.validate_python({**json.loads(raw), 'position': position})

    def opening_before(self, stream: BinaryIO, boundary: int) -> int:
        """Recover the actual opening, including every prompt folded into that occurrence."""
        opening = boundary
        for offset, raw in self.reverse_lines(stream, boundary):
            record = self.decode_line(raw, offset)
            if record.ends_turn():
                break
            opening = record.opening_position(opening)
        return opening

    def record_updates(self, record: ClaudeRecord, state: ClaudeSession, offset: int,
                       raw: bytes) -> Iterator[RuntimeUpdate]:
        yield from state.located(record.cwd)
        for update in (*state.version_reported(record.version), *record.updates(state)):
            yield from update.during(state.phase)


class ClaudeTranscript(ClaudeJsonLines, JsonLinesFile, ClaudeHistory, OfflineHistory):
    """Claude Code writes one transcript file per session (`<id>.jsonl`) and per subagent
    (`<id>/subagents/agent-<id>.jsonl`), under ~/.claude/projects."""

    suffixes = frozenset({'.jsonl'})

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        """The file is named by its session; a subagent's task description is beside it, not in it."""
        if not self.session.holds(self.source.stem):
            raise ValueError('The transcript belongs to a different Claude session.')
        meta = self.source.with_suffix('.meta.json')
        title = (ThreadText.external(SubagentMeta.model_validate_json(meta.read_bytes()).description)
                 if meta.is_file() else NoThreadText())
        yield SessionFacts(source_id=self.source.stem, title=title)

    @staticmethod
    def subagents(session: Path) -> tuple[Path, ...]:
        """The subagent transcripts a stored session started, oldest first."""
        return tuple(sorted(session.with_suffix('').glob('subagents/agent-*.jsonl'),
                            key=lambda path: path.stat().st_mtime))


class RunningClaude(ClaudeTranscript, OnlineHistory):
    """A session Claude Code is writing, found by its session id. Claude has no server to ask,
    so the running session is read from the same file as a stored one."""

    def __init__(self, thread: str) -> None:
        matches = tuple((Path.home() / '.claude/projects').glob(f'*/{thread}.jsonl'))
        if len(matches) != 1:
            raise ValueError(f'Expected one Claude transcript for {thread}; found {len(matches)}')
        super().__init__(matches[0], NamedSession(thread))

    @classmethod
    def connected(cls, thread: str, address: HarnessAddress, directory: Path) -> Self:
        return cls(thread)
