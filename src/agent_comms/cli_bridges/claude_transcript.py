"""Claude Code transcript files, read whether stored or still being written; no bridge."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from abc import ABC
from typing import TYPE_CHECKING, BinaryIO, ClassVar, Literal, Self

from agent_comms.runtime_updates import RuntimeUpdate, SessionFacts
from agent_comms.thread_text import NoThreadText, ThreadText

from ..session_receipts import SessionReceipts
from ..cli_protocol import TaggedWire
from ..wire_models import WireModel
from .shared import WireReference
from .claude_protocol import CLAUDE_RECORD, ClaudeRecord, ClaudeSession, SubagentProgress
from .history import (
    ClaudeHistory, HarnessAddress, JsonLines, JsonLinesFile, NamedSession,
    OfflineHistory, OnlineHistory,
)

if TYPE_CHECKING:
    from ..inbox_watch import PushedTranscript


class SubagentMeta(WireModel):
    description: str


class ClaudeJsonLines(JsonLines):
    """How a Claude Code transcript file decodes, whether stored or still being written."""

    records = CLAUDE_RECORD
    state = ClaudeSession

    def decode_line(self, raw: bytes, position: int) -> ClaudeRecord:
        return self.records.validate_python({**json.loads(raw), 'position': position})

    def receipts(self, lines: Iterable[bytes]) -> SessionReceipts:
        """What these whole transcript lines show the session did, oldest first."""
        receipts = SessionReceipts()
        for position, raw in enumerate(lines):
            record = self.decode_line(raw, position)
            record.tally(receipts)
            if record.ends_turn():
                receipts.turn_ended()
        receipts.turn_ended()
        return receipts

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

    def progress(self) -> SubagentProgress:
        """Where the newest record that decides it says this agent stands, read newest first
        without the older prefix; a transcript with no such record is malformed and fails."""
        with self.source.open('rb') as stream:
            return next(progress for offset, raw in self.reverse_lines(stream, stream.seek(0, 2))
                        for progress in self.decode_line(raw, offset).subagent_progress())

    @staticmethod
    def subagents(session: Path) -> tuple[Path, ...]:
        """The subagent transcripts a stored session started, oldest first."""
        return tuple(sorted(session.with_suffix('').glob('subagents/agent-*.jsonl'),
                            key=lambda path: path.stat().st_mtime))


WINDOW_ENDED = ""
"""On a watch socket: the recent window has been sent and what follows is live. A transcript line is
never blank (JsonLines.complete_lines), so this cannot be mistaken for one."""

WATCH_STOPPED = 4000


class WatchReport(TaggedWire, ABC, affix="", declared_name=None):
    """What a watch client says up its socket besides its transcript: the messages it may have
    shown. A reference is written to the client's ledger before its event is printed, so the
    ledger can only overstate what was shown; a message missing from it was never printed."""

    protocol: Literal['agent-comms-watch'] = 'agent-comms-watch'
    wire_prefix: ClassVar[str] = '{"protocol":' + json.dumps(protocol) + ','
    printed: tuple[WireReference, ...]

    def line(self) -> str:
        return self.model_dump_json()

    async def received(self, pushed: PushedTranscript) -> None:
        """Each printed message's attempt is emitted: shown or about to be, never sent again."""
        for reference in self.printed:
            await pushed.emitted(reference)


class PrintedNow(WatchReport, declared_name='printed'):
    """The message just printed under the session's Monitor."""


class PrintedLedger(WatchReport, declared_name='ledger'):
    """Everything this machine's clients may have printed, sent first on every connect. Once it
    is in, an attempt still unconfirmed provably never reached a Monitor and may go again."""

    async def received(self, pushed: PushedTranscript) -> None:
        await super().received(pushed)
        pushed.ledger_read.set()


WATCH_REPORT = WireModel.adapter(WatchReport.declared_union())
"""On a watch socket: the close code saying this thread was stopped on the bus, so the session must
not re-arm. It is in the application range (4000-4999): a re-armed watch replacing an older socket
closes with 1000, and an install drops the socket (1006), and both of those mean re-arm."""


class RecentClaudeWindow(ClaudeJsonLines):
    """Where a running session's transcript is read from: its recent window's owning occurrence,
    never an arbitrary mid-turn prefix."""

    RECENT_BYTES: ClassVar[int] = 4 * 1024 * 1024

    def history_start(self, stream: BinaryIO, boundary: int) -> int:
        start = max(0, boundary - self.RECENT_BYTES)
        if start:
            stream.seek(start - 1)
            stream.readline()
            start = stream.tell()
        return self.opening_before(stream, start)


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
