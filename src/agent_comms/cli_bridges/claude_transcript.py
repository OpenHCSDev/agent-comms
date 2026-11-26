"""Claude Code transcript files, read whether stored or still being written; no bridge."""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, ClassVar, Literal, Self

from agent_comms.cli_bridges.claude_protocol import (
    CLAUDE_RECORD,
    ClaudeMonitorFrame,
    ClaudeRecord,
    ClaudeSession,
    SubagentProgress,
)
from agent_comms.cli_bridges.cli_protocol import TaggedWire
from agent_comms.cli_bridges.harness_delivery_states import PrintLedgerId, TakingLedger
from agent_comms.cli_bridges.history import (
    ClaudeHistory,
    HarnessAddress,
    JsonLines,
    JsonLinesFile,
    OfflineHistory,
    OnlineHistory,
)
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate, SessionFacts
from agent_comms.cli_bridges.session_ledger import SessionLedger
from agent_comms.cli_bridges.shared import WireReference
from agent_comms.cli_bridges.wire_models import WireModel
from agent_comms.comms import Comms
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.message_files import MessageFile, MessageFileContent
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.nominal.field_codec import FieldCodec, StoredSessionId
from agent_comms.registry.thread_text import NamedSession, NoThreadText, ThreadText

class SubagentMeta(WireModel):
    description: str


class ClaudeJsonLines(JsonLines[ClaudeRecord]):
    """How a Claude Code transcript file decodes, whether stored or still being written."""

    records = CLAUDE_RECORD
    state = ClaudeSession

    def decode_line(self, raw: bytes, position: int) -> ClaudeRecord:
        return self.records.validate_python({**json.loads(raw), 'position': position})

    def ledger(self, lines: Iterable[bytes]) -> SessionLedger:
        """What these whole transcript lines show the session did, oldest first."""
        ledger = SessionLedger()
        for position, raw in enumerate(lines):
            record = self.decode_line(raw, position)
            for _ in record.owner_words():
                ledger.spoken_to()
            record.tally(ledger)
            if record.ends_turn():
                ledger.turn_ended()
        ledger.turn_ended()
        return ledger

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
        if (located := state.located(record.cwd)) is not None:
            yield located
        for update in (*state.version_reported(record.version), *record.updates(state)):
            yield from update.during(state.phase)


class ClaudeTranscript(ClaudeJsonLines, JsonLinesFile[ClaudeRecord], ClaudeHistory, OfflineHistory):
    """Claude Code writes one transcript file per session (`<id>.jsonl`) and per subagent
    (`<id>/subagents/agent-<id>.jsonl`), under ~/.claude/projects."""

    suffixes = frozenset({'.jsonl'})

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        """The file is named by its session; a subagent's task description is beside it, not in it."""
        if not self.session.holds(StoredSessionId(self.source.stem)):
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
WATCH_REPLACED = 4001
"""A newer watch of the same thread took this socket's place, as a re-armed watch does."""
WATCH_DEFERRED = 4002
"""The bus is under maintenance, as every install holds it: the socket opened and was handed
its next URL, but the thread cannot be attached until maintenance lifts."""


@dataclass
class ReportedLedger:
    """The printed ledger this socket's client reported, once it has: until then every message
    is taken under no ledger, and nothing it takes can ever be proved unshown."""

    current: TakingLedger = field(default_factory=TakingLedger)
    arrival: asyncio.Event = field(default_factory=asyncio.Event)

    def arrived(self, ledger: PrintLedgerId) -> None:
        self.current = ledger
        self.arrival.set()


class ReceivesWatchReports(ABC):
    """The connector's end of a watch socket, which the client's reports act on."""

    reported: ReportedLedger

    @abstractmethod
    async def emitted(self, printed: frozenset[MessageReference]) -> None:
        """The client printed these, or was about to: their attempts are never sent again."""

    @abstractmethod
    async def reconnectable(self) -> None:
        """The client can reconnect on its own: hand it this watch's standing reconnect URL."""

    @abstractmethod
    async def files_wanted(self, shown: frozenset[MessageReference]) -> None:
        """The client keeps the files of these messages on its own disk: send their bytes."""

    @abstractmethod
    async def stream_antigravity(self) -> None:
        """The client streams Antigravity `transcript.jsonl` records on this socket."""

    @abstractmethod
    async def turn_idle(self) -> tuple[RuntimeUpdate, ...]:
        """The client's live run-state API reported the harness idle; retire any open turn."""


class ReconnectUrl(WireModel):
    """The connector's one frame that is not a message: where this watch's client reconnects
    after a network reset, without its model. It stands until it is redeemed or a newer watch
    of the thread opens. The client keeps it in memory only; it is a bearer credential, so it
    is never printed, logged or written to the ledger."""

    protocol: Literal['agent-comms-watch-reconnect'] = 'agent-comms-watch-reconnect'
    wire_prefix: ClassVar[str] = '{"protocol":' + json.dumps(protocol) + ','
    url: str

    def line(self) -> str:
        return self.model_dump_json()


class WatchedFilePart(WireModel):
    """One piece of one file a pushed message carries, sent to a client that asked for that
    message's files. The session reads a picture from its own disk; the only other read hands
    the bytes back as text inside the conversation, where a phone photo is several million
    characters. The pieces of a file arrive in order and joined they are its base64 text."""

    protocol: Literal['agent-comms-watch-file'] = 'agent-comms-watch-file'
    wire_prefix: ClassVar[str] = '{"protocol":' + json.dumps(protocol) + ','
    message: WireReference
    position: int
    name: str
    mime_type: str
    size: int
    sha256: str
    part: int
    parts: int
    data: str

    PART_CHARS: ClassVar[int] = 1024 * 1024
    """Under the 4 MiB a websocket client takes in one frame by default."""

    @classmethod
    def of(cls, content: MessageFileContent) -> tuple[Self, ...]:
        file, text = content.file, content.data
        pieces = tuple(text[start:start + cls.PART_CHARS]
                       for start in range(0, len(text), cls.PART_CHARS)) or ("",)
        return tuple(cls(message=file.message, position=file.position, name=file.name,
                         mime_type=file.mime_type, size=file.size, sha256=file.sha256,
                         part=number, parts=len(pieces), data=piece)
                     for number, piece in enumerate(pieces, start=1))

    def line(self) -> str:
        return self.model_dump_json()


@dataclass(frozen=True)
class WatchedDelivery:
    """What a watch socket pushes for one message: the message with the files it carries, read
    together and encoded whole, so the server's evidence and the client's printed event are
    built from the same frame and cannot disagree on its parts."""

    message: Delivered
    files: tuple[MessageFile, ...]

    @classmethod
    def read(cls, comms: Comms, messages: Iterable[Message]) -> tuple[Self, ...]:
        """These messages with their files, from one files read for all of them."""
        messages = tuple(messages)
        files = comms.messaging.files.of(message.reference for message in messages)
        return tuple(cls(message, tuple(file for file in files if file.message == message.reference))
                     for message in messages)

    @classmethod
    def parsed(cls, line: str) -> Self:
        return FieldCodec.decode(cls, FieldCodec.parse(line))

    def line(self) -> str:
        return FieldCodec.compact(FieldCodec.encode(self))

    @property
    def frame(self) -> ClaudeMonitorFrame:
        """How a Claude session's Monitor shows it, files named after the body."""
        return ClaudeMonitorFrame(self.message, self.files)


class WatchReportFields(WireModel):
    """The fields every watch report carries, protocol first so its lines start with the
    prefix. They sit on a plain model, as TextDisplay's do, where constructors can see them."""

    protocol: Literal['agent-comms-watch'] = 'agent-comms-watch'
    wire_prefix: ClassVar[str] = '{"protocol":' + json.dumps(protocol) + ','
    printed: tuple[WireReference, ...]


class LedgerFields(WatchReportFields):
    """A ledger report's fields: the common ones, then the ledger's id."""

    ledger: PrintLedgerId


class WatchReport(WatchReportFields, TaggedWire, ABC, affix="", declared_name=None):
    """What a watch client says up its socket besides its transcript: the messages it may have
    shown. A reference is written to the client's ledger before its event is printed, so the
    ledger can only overstate what was shown; a message missing from it was never printed."""

    def line(self) -> str:
        return self.model_dump_json()

    async def received(self, pushed: ReceivesWatchReports) -> tuple[RuntimeUpdate, ...]:
        """Each printed message's attempt is emitted: shown or about to be, never sent again."""
        await pushed.emitted(frozenset(self.printed))
        return ()


class PrintedNow(WatchReport, declared_name='printed'):
    """The message just printed under the session's Monitor."""


class PrintedLedger(LedgerFields, WatchReport, declared_name='ledger'):
    """Everything this session's client may have printed, sent first on every connect, with the
    ledger's own id. Once it is in, an attempt this ledger took and still unconfirmed provably
    never reached a Monitor and may go again."""

    async def received(self, pushed: ReceivesWatchReports) -> tuple[RuntimeUpdate, ...]:
        await super().received(pushed)
        pushed.reported.arrived(self.ledger)
        return ()


class ReconnectsAlone(WatchReport, declared_name='reconnects'):
    """This client re-dials a reset socket itself, so it can take a ReconnectUrl frame. A client
    that never says so gets only messages, which is all an older client can decode."""

    async def received(self, pushed: ReceivesWatchReports) -> tuple[RuntimeUpdate, ...]:
        await super().received(pushed)
        await pushed.reconnectable()
        return ()


class FilesWanted(WatchReport, declared_name='files'):
    """The messages just shown carry files, and this client writes a message's files to its
    session's disk: send their bytes as WatchedFilePart frames. A client that never says so
    gets only messages, which is all an older client can decode."""

    async def received(self, pushed: ReceivesWatchReports) -> tuple[RuntimeUpdate, ...]:
        await pushed.files_wanted(frozenset(self.printed))
        return ()


class AntigravityWatch(WatchReport, declared_name='antigravity'):
    """This watch client streams Antigravity `transcript.jsonl` records instead of Claude JSONL."""

    async def received(self, pushed: ReceivesWatchReports) -> tuple[RuntimeUpdate, ...]:
        await pushed.stream_antigravity()
        return ()


class TurnIdle(WatchReport, declared_name='turn_idle'):
    """The harness's live run-state API reported the session idle while its transcript had no
    closing record (e.g. quota exhaustion, API error, or cancellation)."""

    async def received(self, pushed: ReceivesWatchReports) -> tuple[RuntimeUpdate, ...]:
        return await pushed.turn_idle()


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
        """Claude writes a session again under another project directory when it restarts
        in a new working directory; the one it is writing now is the newest."""
        matches = tuple((Path.home() / '.claude/projects').glob(f'*/{thread}.jsonl'))
        if not matches:
            raise ValueError(f'No Claude transcript for {thread}')
        super().__init__(max(matches, key=lambda path: path.stat().st_mtime), NamedSession(thread))

    @classmethod
    def connected(cls, thread: str, address: HarnessAddress, directory: Path) -> Self:
        return cls(thread)
