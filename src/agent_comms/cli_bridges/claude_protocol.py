"""Claude's completed JSONL blocks own transcript items and activity."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self
from xml.etree import ElementTree

from pydantic import (
    Field,
    JsonValue,
    ModelWrapValidatorHandler,
    field_validator,
    model_validator,
)

from agent_comms.agent_events import TranscriptModelUsage
from agent_comms.cli_bridges.cli_protocol import (
    Declared,
    HistoryRecord,
    OpenWire,
    TaggedWire,
    TextWire,
    TimestampedWire,
    WireHeader,
)
from agent_comms.cli_bridges.runtime_updates import (
    ActiveItem,
    AllTurnsCancelled,
    AllTurnsEnded,
    FinishedItem,
    HiddenItem,
    HistoryNotice,
    OfferedToolsObserved,
    ReadingPhase,
    RuntimeInfoObserved,
    RuntimeUpdate,
    SessionFacts,
    StartedItem,
    StatusUpdate,
    Summarized,
    ToolDraft,
    TurnEnded,
    TurnStarted,
)
from agent_comms.cli_bridges.session_receipts import SessionReceipts
from agent_comms.cli_bridges.shared import (
    MONITOR_FRAME,
    ConfirmedDelivery,
    ForeignMonitorFrame,
    MonitorFrame,
    MonitorIncomingFrame,
    MonitorObservation,
    MonitorPart,
    MonitorProtocolFrame,
    MonitorReadFile,
    ObservedMonitor,
    TranscriptConfirmsDelivery,
    WireReference,
)
from agent_comms.cli_bridges.tool_calls import CliToolArguments, ToolCall
from agent_comms.cli_bridges.wire_models import WireModel
from agent_comms.execution.turn_item import (
    NamedSourceTurn,
    NativeItemId,
    NativeTurnId,
    NoSourceTurn,
    SourceTurn,
)
from agent_comms.execution.turn_phase import ModelWaitPhase, ToolRunningPhase, TurnPhase
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import (
    MalformedRecord,
    ToolCallId,
    TranscriptRecordId,
)
from agent_comms.pi_counts import PiCount, PiCountWire
from agent_comms.registry.thread_identity import OfferedTools, ToolName, UnobservedTools
from agent_comms.registry.thread_model import ThreadModel
from agent_comms.registry.thread_text import ThreadText
from agent_comms.storage.recorded_time import EventTime
from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    NarrationTranscript,
    NoticeTranscript,
    ThinkingTranscript,
    TranscriptEvent,
    UserTranscript,
)
from agent_comms.transport.tool_declaration import ContextRows, one_line

if TYPE_CHECKING:
    from agent_comms.cli_bridges.shared import MonitorAttachment


class ClaudeMonitorFrame(MonitorIncomingFrame):
    """Claude Monitor caps lines at 500 and joined batches at 3000 UTF-16 units, trims every
    line and drops one left empty. A message is shown as written across the lines of one
    batch when the batch holds it, wrapped under the line cap; any other goes to a file."""
    EVENT_CHARS = 500
    BATCH_CHARS = 3000
    READ_LINE_CHARS = 2000
    HEADER = 'Comms in'
    HEADER_PREFIX = HEADER + ' · '
    """How a shown message's first line starts; no body line shown inline may start so."""
    GUARD = '\u200b'
    """A blank line would vanish; a zero-width space keeps it and reads as nothing. Nobody
    copies a blank line, so the guard goes nowhere else: an indent would carry it into whatever
    a reader pastes, so an indented body is the file instead."""
    XML_UNSAFE = re.compile('[^\t\x20-퟿-�\U00010000-\U0010ffff]')
    """What a transcript's XML envelope cannot carry as written: control characters, CR and
    the two noncharacters."""

    @staticmethod
    def units(line: str) -> int:
        return len(line.encode('utf-16-le')) // 2

    @property
    def header(self) -> str:
        """Where it was posted (a channel, or the thread a direct message went to), who sent
        it as the reader sees them, then the reference a reply cites and decode_header reads
        back. Tristan, 2026-11-06: "theres chcannel messags and dm messages"."""
        return (f'{self.HEADER_PREFIX}{self.message.target} · {self.message.sender_label} · '
                f'{self.message.reference.name}\n')

    @cached_property
    def inline(self) -> str:
        return self.header + self.message.body

    @cached_property
    def shown_lines(self) -> tuple[str, ...]:
        """The header and body lines, with any line Monitor would cut wrapped after its last
        space inside the cap, or at the cap when there is none, and blank lines guarded."""
        return tuple(self.guarded(piece) for line in self.inline.split('\n')
                     for piece in self.wrapped(line))

    @classmethod
    def guarded(cls, line: str) -> str:
        return line if line.strip() else cls.GUARD

    @classmethod
    def wrapped(cls, line: str) -> tuple[str, ...]:
        pieces: list[str] = []
        while cls.units(line) > cls.EVENT_CHARS:
            end = cls.EVENT_CHARS
            while cls.units(line[:end]) > cls.EVENT_CHARS:
                end -= 1
            space = line.rfind(' ', 0, end)
            cut = space + 1 if space > 0 else end
            pieces.append(line[:cut])
            line = line[cut:]
        return (*pieces, line)

    @cached_property
    def whole_part(self) -> MonitorPart:
        """The one part an inline event shows: the whole message."""
        return MonitorPart.model_validate(dict(
            id=self.message.message_id, sender=self.message.sender, number=1, total=1,
            body=self.message.body))

    @cached_property
    def observed(self) -> tuple[str, ...]:
        """The event as a transcript records it: Monitor trims each line's end."""
        return tuple(line.rstrip() for line in self.shown_lines)

    def shown_inline(self) -> str:
        return '\n'.join(self.shown_lines)

    @cached_property
    def fits_one_event(self) -> bool:
        return self.units(self.shown_inline()) <= self.BATCH_CHARS and self.shows_safely_inline

    @property
    def shows_safely_inline(self) -> bool:
        """Each shown line is a line of the event, and the transcript decoder reads every line:
        a body line that would read as a header or one of our frames goes to the file, where
        nothing decodes it. Monitor trims a line's indent, so an indented body goes there too,
        where a reader copies it byte for byte. The transcript records every other character
        verbatim inside an XML envelope, which cannot hold a control character raw and reads a
        CR as a line break, so a body with one of those goes to the file as well."""
        return not any(self.claims(line) or line[:1].isspace() or self.XML_UNSAFE.search(line)
                       for line in self.shown_lines[1:])

    @cached_property
    def slices(self) -> tuple[str, ...]:
        """The body as file lines: each keeps its own LF, so joined they are the body. Only a
        line longer than Read can show is cut, because Read cannot page within one line."""
        return tuple(
            line[start:start + self.READ_LINE_CHARS]
            for line in re.findall(r'[^\n]*\n|[^\n]+', self.message.body)
            for start in range(0, len(line), self.READ_LINE_CHARS)
        )

    @cached_property
    def parts(self) -> tuple[str, ...]:
        return (self.inline,) if self.fits_one_event else self.slices

    @cached_property
    def export_lines(self) -> tuple[str, ...]:
        """The readable message: who sent it, then its lines as written."""
        return (self.header.removesuffix('\n'), *(part.removesuffix('\n') for part in self.slices))

    def read_parts(self, rows: tuple[tuple[int, str], ...]) -> tuple[MonitorPart, ...]:
        """The parts a Read showed, judged against the bus message and never decoded from the
        file. Any shown row that is not this message's line at that number confirms nothing.
        Only one empty row right after the final LF is Read's terminator."""
        lines = self.export_lines
        shown = rows[:-1] if rows and rows[-1] == (len(lines) + 1, '') else rows
        if any(not 1 <= number <= len(lines) or lines[number - 1] != text for number, text in shown):
            return ()
        return tuple(MonitorPart.model_validate(dict(
                         id=self.message.message_id, sender=self.message.sender,
                         number=number - 1, total=len(self.slices),
                         body=self.slices[number - 2]))
                     for number, _ in shown if number > 1)

    def event(self, exports: Path) -> tuple[int, str]:
        """The message's one Monitor event and the reservation it fills. A body one batch holds
        goes inline. Any larger body is the private file the one notice names, whole and
        readable, so no piece reaches the model on its own."""
        if self.fits_one_event:
            return 1, self.shown_inline()
        notice = MonitorReadFile.naming(exports, self.message.reference)
        line = notice.model_dump_json()
        if self.units(line) > self.EVENT_CHARS:
            raise ValueError(f'The read notice for {self.message.message_id} exceeds one '
                             f'Monitor event: {notice.path}')
        notice.export(self.export_lines)
        return notice.reservation, line

    async def emit_to(self, attachment: MonitorAttachment, window_bytes: int, exports: Path) -> None:
        number, line = await asyncio.to_thread(self.event, exports)
        await attachment.emit(self.message.message_id, number, line, window_bytes)

    @classmethod
    def claims(cls, line: str) -> bool:
        """Whether the transcript decoder reads this line as one of ours rather than body text."""
        return line.startswith((MonitorProtocolFrame.wire_prefix, cls.HEADER_PREFIX))

    @classmethod
    def decode_line(cls, line: str) -> MonitorFrame:
        """One line read alone: a header starts a shown message; old or unrelated monitor text,
        including truncated text, is a named foreign frame."""
        if line.startswith(cls.HEADER_PREFIX):
            return cls.decode_header(line)
        if not line.startswith(MonitorProtocolFrame.wire_prefix):
            return ForeignMonitorFrame(text=line)
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return ForeignMonitorFrame(text=line)
        return MONITOR_FRAME.validate_json(line)

    @classmethod
    def decode_header(cls, line: str) -> MonitorFrame:
        try:
            reference = MessageReference.named(line.rpartition(' · ')[2])
        except MalformedRecord:
            return ForeignMonitorFrame(text=line)
        return MonitorShown.model_validate(dict(reference=reference, lines=(line,)))

    @classmethod
    def decode_event(cls, event: str) -> tuple[MonitorFrame, ...]:
        """Monitor joins the lines of one batch with LF, and one batch can hold several of our
        messages: each line continues the frame before it, which a shown message does by taking
        it as body."""
        first, *rest = event.split('\n')
        frames: tuple[MonitorFrame, ...] = (cls.decode_line(first),)
        for line in rest:
            frames = (*frames[:-1], *frames[-1].continued(line, cls.decode_line))
        return frames


class MonitorShown(MonitorFrame, declared_name='shown'):
    """A message shown inline: its lines as the transcript recorded them, header first. Wrapping
    and Monitor's trimming leave no body to decode, so the original is rendered again and
    compared, header and all; only an exact match proves the whole message reached the model."""

    reference: WireReference
    lines: tuple[str, ...]

    def delivered(self) -> tuple[MessageReference, ...]:
        return (self.reference,)

    def continued(
        self, line: str, decode: Callable[[str], MonitorFrame],
    ) -> tuple[MonitorFrame, ...]:
        """A line that is none of ours is this message's next body line."""
        if ClaudeMonitorFrame.claims(line):
            return super().continued(line, decode)
        return (self.model_copy(update=dict(lines=(*self.lines, line))),)

    async def confirm(
        self, bridge: TranscriptConfirmsDelivery, record: ObservedMonitor,
    ) -> tuple[MessageReference, ...]:
        """The one whole part this event proves, confirmed as any other part; an event that is
        not exactly the original's rendering confirms nothing and says so. Only
        ClaudeMonitorFrame decodes a shown message, so it renders the original too."""
        identity = self.reference.message_id
        with bridge.uncertain_on_contention(identity):
            for delivery in await bridge.attempts(identity):
                message = await bridge.message(delivery.reference)
                observed = tuple(line.rstrip() for line in self.lines)
                if ClaudeMonitorFrame(message).observed != observed:
                    await bridge.record_mismatch(
                        delivery, 'Shown lines do not equal the original message')
                    continue
                return await ClaudeMonitorFrame(message).whole_part.confirm(bridge, record)
        return ()


class ClaudeReaderPhase(ReadingPhase):
    """The initial bounded window and live tail have different guarantees."""

    @abstractmethod
    def window_ended(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]: ...

    @abstractmethod
    def missing_tool(self, tool_id: str) -> tuple[RuntimeUpdate, ...]: ...

    @abstractmethod
    def unturned_finish(self) -> tuple[RuntimeUpdate, ...]: ...


@dataclass
class CatchUp(ClaudeReaderPhase):
    pending: tuple[StatusUpdate, ...] = ()
    latest_tools: tuple[OfferedToolsObserved, ...] = ()
    latest_info: tuple[RuntimeInfoObserved, ...] = ()

    def offered(self, update: OfferedToolsObserved) -> tuple[RuntimeUpdate, ...]:
        self.latest_tools = (update,)
        return ()

    def runtime_info(self, update: RuntimeInfoObserved) -> tuple[RuntimeUpdate, ...]:
        self.latest_info = (update,)
        return ()

    def status(self, update: StatusUpdate) -> tuple[RuntimeUpdate, ...]:
        self.pending = (update,)
        return ()

    def window_ended(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        session.phase = Live()
        return (*self.latest_tools, *self.latest_info, *self.pending)

    def missing_tool(self, tool_id: str) -> tuple[RuntimeUpdate, ...]:
        return ()

    def unturned_finish(self) -> tuple[RuntimeUpdate, ...]:
        return ()


class Live(ClaudeReaderPhase):
    def status(self, update: StatusUpdate) -> tuple[RuntimeUpdate, ...]:
        return (update,)

    def window_ended(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def missing_tool(self, tool_id: str) -> tuple[RuntimeUpdate, ...]:
        raise ValueError(f'Claude result has no tool start: {tool_id}')

    def unturned_finish(self) -> tuple[RuntimeUpdate, ...]:
        raise ValueError('Claude completed an item before identifying its turn')


@dataclass
class ClaudeToolDraft(ToolDraft):
    """One decoded tool start, preserved until its separately timed result arrives."""

    call: ToolCall

    def completed(
        self, record: TimedRecord, text: str, ok: bool,
    ) -> tuple[TranscriptEvent, ...]:
        return (*self.events, self.call.ended(text, ok, record.position, record.timestamp))

    def read_results(self, record: TimedRecord, text: str, ok: bool,
                     reading: frozenset[str], source_turn: SourceTurn) -> tuple[RuntimeUpdate, ...]:
        """What a Read showed the model, timed by its own result record, beside the paths other
        Reads in this session still have open. A failed Read showed no file but ends its call."""
        observed = ReadResultObserved if ok else FailedReadObserved
        return tuple(observed(path, text, ObservedMonitor(record.uuid, record.timestamp), reading,
                              source_turn=source_turn, layout=self.call.context_rows)
                     for path in self.call.context_read_paths())


class ClaudeTurn(ABC):
    """The transcript's identified turn owns item identities."""

    def opened(self, record: TimedRecord) -> ClaudeTurn:
        """Further prompt records belong to the turn already running."""
        return self

    @property
    def source(self) -> SourceTurn:
        return NoSourceTurn()

    @abstractmethod
    def started(self, tool: ClaudeToolDraft,
                phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]: ...

    @abstractmethod
    def finished(self, item: str, events: tuple[TranscriptEvent, ...],
                 phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]: ...

    @abstractmethod
    def active(self, tool_id: str, phase: ClaudeReaderPhase,
               turn_phase: TurnPhase) -> tuple[RuntimeUpdate, ...]: ...


class UnknownTurn(ClaudeTurn):
    def opened(self, record: TimedRecord) -> ClaudeTurn:
        return KnownTurn(NativeTurnId(record.uuid))

    def started(self, tool: ClaudeToolDraft,
                phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]:
        return phase.unturned_finish()

    def finished(self, item: str, events: tuple[TranscriptEvent, ...],
                 phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]:
        return phase.unturned_finish()

    def active(self, tool_id: str, phase: ClaudeReaderPhase,
               turn_phase: TurnPhase) -> tuple[RuntimeUpdate, ...]:
        return phase.unturned_finish()


@dataclass(frozen=True)
class KnownTurn(ClaudeTurn):
    id: NativeTurnId

    @property
    def source(self) -> SourceTurn:
        return NamedSourceTurn(self.id)

    def started(self, tool: ClaudeToolDraft,
                phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]:
        return (StartedItem(self.id, NativeItemId(tool.call.id),
                            ToolRunningPhase(tools=((tool.call.id, tool.call.title),)), tool),)

    def finished(self, item: str, events: tuple[TranscriptEvent, ...],
                 phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]:
        return (FinishedItem(turn_id=self.id, item_id=NativeItemId(item), events=events),)

    def active(self, tool_id: str, phase: ClaudeReaderPhase,
               turn_phase: TurnPhase) -> tuple[RuntimeUpdate, ...]:
        return (ActiveItem(self.id, NativeItemId(tool_id), turn_phase),)


@dataclass
class ClaudeSession:
    turn: ClaudeTurn = field(default_factory=UnknownTurn)
    tools: dict[str, ClaudeToolDraft] = field(default_factory=dict)
    phase: ClaudeReaderPhase = field(default_factory=CatchUp)
    project: str = ''
    offered_tools: OfferedTools = field(default_factory=UnobservedTools)
    answered_by: TranscriptModelUsage = field(default_factory=TranscriptModelUsage)

    def version_reported(self, version: str | None) -> tuple[RuntimeUpdate, ...]:
        if version is None:
            return ()
        return tuple(update for offered in self.offered_tools.on_version(version)
                     for update in self.offered(offered))

    def offered(self, offered: OfferedTools) -> tuple[RuntimeUpdate, ...]:
        self.offered_tools = offered
        return (OfferedToolsObserved(offered),)

    def answered(self, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]:
        """A model call reports its model and usage once; Claude repeats them on every block."""
        for used in message.usage.context_used.present():
            observation = TranscriptModelUsage(
                model=ThreadModel.reported(message.model), context_used=used)
            if observation != self.answered_by:
                self.answered_by = observation
                return (RuntimeInfoObserved(observation),)
        return ()

    def located(self, cwd: str) -> tuple[RuntimeUpdate, ...]:
        if not cwd or cwd == self.project:
            return ()
        self.project = cwd
        return (SessionFacts(project=cwd),)

    def started(self, tool: ClaudeToolDraft) -> tuple[RuntimeUpdate, ...]:
        self.tools[tool.call.id] = tool
        return self.turn.started(tool, self.phase)

    def tool_result(
        self, tool_id: str, text: str, ok: bool, record: TimedRecord,
    ) -> tuple[RuntimeUpdate, ...]:
        try:
            tool = self.tools.pop(tool_id)
        except KeyError:
            return self.phase.missing_tool(tool_id)
        return (*self.finished(tool_id, tool.completed(record, text, ok)),
                *self.turn.active(tool_id, self.phase, ModelWaitPhase()),
                *tool.read_results(record, text, ok, frozenset(
                    path for other in self.tools.values()
                    for path in other.call.context_read_paths()), self.turn.source))

    def open_turn(self, record: TimedRecord) -> None:
        self.turn = self.turn.opened(record)

    def finished(self, item: str, events: tuple[TranscriptEvent, ...]) -> tuple[RuntimeUpdate, ...]:
        return self.turn.finished(item, events, self.phase)

    def retired(self, ending: TurnEnded) -> tuple[RuntimeUpdate, ...]:
        self.turn = UnknownTurn()
        return (ending,)


class SubagentProgress(ABC):
    """Where a Claude subagent stands, read from the newest transcript record that says it."""

    def owing(self) -> tuple[OwedProgress, ...]:
        """An agent that answered, or was interrupted, owes no next call."""
        return ()


@dataclass(frozen=True)
class AnsweredSubagent(SubagentProgress):
    """Its newest deciding record is a final answer or an interruption: it has stopped."""


@dataclass(frozen=True)
class OwedProgress(SubagentProgress, ABC):
    """It is mid-turn: its next record is owed since ``at``."""

    at: float

    def owing(self) -> tuple[OwedProgress, ...]:
        return (self,)

    @abstractmethod
    def doing(self, now: float) -> str:
        """What it last did, as the board names it."""


@dataclass(frozen=True)
class CallingSubagent(OwedProgress):
    """Its newest deciding record is a tool call: the call or the model after it is running."""

    call: ToolCall

    def doing(self, now: float) -> str:
        return (f"its last tool call was {(now - self.at) / 60:.0f} min ago:"
                f" {one_line(self.call.title, 200)}")


@dataclass(frozen=True)
class PromptedSubagent(OwedProgress):
    """Its newest deciding record is a prompt: it has made no tool call since."""

    def doing(self, now: float) -> str:
        return f"it was prompted {(now - self.at) / 60:.0f} min ago and has made no tool call since"


class TimedRecord(TimestampedWire):
    position: int = Field(exclude=True)
    uuid: TranscriptRecordId


class ClaudeUserHeader(WireHeader):
    """Claude's sparse user header; Pydantic owns which fields were supplied."""

    prompt_id: str = Field(default='', alias='promptId')
    origin: KindHeader = Field(default_factory=lambda: KindHeader(kind=''))
    is_compact_summary: bool = Field(default=False, alias='isCompactSummary')
    is_meta: bool = Field(default=False, alias='isMeta')
    turn_origin: str = Field(default='', alias='turnOrigin')

    def record_type(self) -> str:
        if self.origin.type == TaskNotification.declared_name:
            return TaskUserRecord.declared_name
        if 'prompt_id' not in self.model_fields_set:
            return LocalCommandOutput.declared_name
        if self.is_compact_summary:
            return CompactSummary.declared_name
        if self.is_meta and not {'origin', 'turn_origin'} & self.model_fields_set:
            return HarnessNote.declared_name
        return UserRecord.declared_name


class ClaudeHeader(WireHeader):
    @model_validator(mode='wrap')
    @classmethod
    def local_command(cls, value: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        header = handler(value)
        if header.type == UserRecord.declared_name:
            header.type = ClaudeUserHeader.model_validate(value).record_type()
        return header


class ClaudeRecord(TaggedWire, HistoryRecord, affix=""):
    header = ClaudeHeader
    cwd: str = ''
    version: str | None = None

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def compacted_at(self) -> tuple[float, ...]:
        """When this record says the context compacted: nothing, unless it is the boundary."""
        return ()

    def ends_turn(self) -> bool:
        return False

    def opening_position(self, following: int) -> int:
        return following

    def delivered(self) -> tuple[MessageReference, ...]:
        """The bus messages this record put in front of the model; most records carry none."""
        return ()

    def said(self) -> tuple[str, ...]:
        """What the model said to its reader in this record; most records carry none."""
        return ()

    def subagent_progress(self) -> tuple[SubagentProgress, ...]:
        """Where this record says its agent stands; metadata and notices say nothing."""
        return ()

    def tally(self, receipts: SessionReceipts) -> None:
        """What this record adds to the session's receipts; most records add nothing."""


class AssistantBlock(TaggedWire, ABC, affix="", declared_name=None):

    @classmethod
    def undeclared_tag(cls, tag: str) -> str:
        """Claude Code adds content-block tags; an unread one keeps its record readable."""
        return UnreadableBlock.tag()

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def events(self, record: TimedRecord) -> tuple[TranscriptEvent, ...]:
        return ()

    def said(self) -> tuple[str, ...]:
        """The words this block shows the reader; a tool call or hidden reasoning shows none."""
        return ()

    def tally(self, receipts: SessionReceipts) -> None:
        receipts.said(self.said())

    def subagent_progress(self, record: TimedRecord) -> tuple[SubagentProgress, ...]:
        """Reasoning decides nothing: the text or call it precedes does."""
        return ()


class UnreadableBlock(AssistantBlock, declared_name='__undeclared__'):
    """A content block whose tag this version does not declare.

    Claude Code writes one when the harness replaces a response, and adds new
    tags over time. The block shows the reader nothing and moves no lifecycle,
    so the records around it still reach the bus.
    """


class TextContent(WireModel):
    text: str

    def result_text(self) -> str:
        return self.text


class ImageContent(WireModel):
    def result_text(self) -> str:
        return '[image]'


class AssistantText(TextWire, AssistantBlock, TextContent, declared_name='text'):

    def subagent_progress(self, record: TimedRecord) -> tuple[SubagentProgress, ...]:
        """Text with no call after it in its message is the answer."""
        return (AnsweredSubagent(),)

    def said(self) -> tuple[str, ...]:
        return (self.text,)

    def events(self, record: TimedRecord) -> tuple[TranscriptEvent, ...]:
        return (AssistantTranscript(self.text, timestamp=record.timestamp, position=record.position),)


class RedactedThinking(AssistantBlock, declared_name='redacted_thinking'):

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.turn.active(record.uuid, session.phase, ModelWaitPhase())

    def events(self, record: TimedRecord) -> tuple[TranscriptEvent, ...]:
        return (ThinkingTranscript('', timestamp=record.timestamp, position=record.position),)


class NarrationBlock(RedactedThinking, declared_name='thinking'):
    """Claude Code saves its reasoning under this tag with empty text, and the
    narration it shows before a tool call under the same tag with the text
    filled in. The text is a message to the reader, never hidden reasoning."""

    thinking: str

    def said(self) -> tuple[str, ...]:
        """Saved reasoning is empty; narration is what the reader saw."""
        return (self.thinking,) if self.thinking else ()

    def events(self, record: TimedRecord) -> tuple[TranscriptEvent, ...]:
        if not self.thinking:
            return super().events(record)
        return (NarrationTranscript(self.thinking, timestamp=record.timestamp, position=record.position),)


class ToolUse(AssistantBlock, declared_name='tool_use'):
    id: ToolCallId
    name: str
    input: dict[str, JsonValue]

    def tally(self, receipts: SessionReceipts) -> None:
        receipts.called(str(self.id), self.name)

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        call = self.call()
        return session.started(ClaudeToolDraft(
            EventTime.capture(record.timestamp),
            (call.started(record.position, record.timestamp),), call))


    def call(self) -> ToolCall:
        return CliToolArguments.decode(ToolCall(self.id, self.name, self.input), self.input)

    def subagent_progress(self, record: TimedRecord) -> tuple[SubagentProgress, ...]:
        return (CallingSubagent(record.timestamp, self.call()),)


class ClaudeUsage(WireModel):
    """Tokens one model call read; their sum is the context the next call starts from."""

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def context_used(self) -> PiCount:
        """Claude's local notices carry zero usage: no model call, nothing reported."""
        return PiCountWire.decode(
            self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens)


class AssistantMessage(WireModel):
    content: list[Declared[AssistantBlock]]
    model: str = ''
    usage: ClaudeUsage = Field(default_factory=ClaudeUsage)


class AssistantRecord(ClaudeRecord, TimedRecord, declared_name='assistant'):
    message: AssistantMessage

    def said(self) -> tuple[str, ...]:
        return tuple(words for block in self.message.content for words in block.said())

    def tally(self, receipts: SessionReceipts) -> None:
        for block in self.message.content:
            block.tally(receipts)

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        """Claude can answer with no prompt record of its own, right after the last turn's
        duration: a queued notification is one way. The assistant record then opens the turn."""
        session.open_turn(self)
        updates = [update for block in self.message.content for update in block.updates(self, session)]
        events = tuple(event for block in self.message.content for event in block.events(self))
        if events:
            updates.extend(session.finished(self.uuid, events))
        return (*updates, *session.answered(self.message))

    def subagent_progress(self) -> tuple[SubagentProgress, ...]:
        """Its last deciding block, so text before a call reads as the call."""
        return tuple(progress for block in reversed(self.message.content)
                     for progress in block.subagent_progress(self))


class ResultPart(TaggedWire, ABC, affix=""):

    @abstractmethod
    def result_text(self) -> str: ...


class ResultText(TextWire, TextContent, ResultPart, declared_name='text'):
    pass


class ResultImage(ImageContent, ResultPart, declared_name='image'):
    pass


class ResultToolReference(ResultPart, declared_name='tool_reference'):
    tool_name: str

    def result_text(self) -> str:
        return f'Tool available: {self.tool_name}'


class KindHeader(WireHeader):
    type: str = Field(alias='kind')


class PromptOrigin(TaggedWire, ABC, affix=""):
    """Who wrote a prompt. Only a person's prompt is shown live as input;
    another agent's message is conversation history the live view hides."""
    header = KindHeader

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def absorbed(self, record: TimedRecord, text: str,
                 session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        """The prompt Claude folded into the turn already running."""
        return ()

    def request(self, text: str) -> str:
        return ''

    def owner_words(self, text: str) -> tuple[str, ...]:
        """The words the person typed; only a person's prompt carries any."""
        return ()

    def delivered(self, text: str) -> tuple[MessageReference, ...]:
        """The bus messages this prompt carried; only a watch's task notification carries any."""
        return ()


class Prompting(PromptOrigin, ABC, declared_name=None):
    """An origin whose text is a request to the model."""

    def request(self, text: str) -> str:
        return text

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (TurnStarted(), *self.absorbed(record, text, session))

    def absorbed(self, record: TimedRecord, text: str,
                 session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (HiddenItem((UserTranscript(text, timestamp=record.timestamp, position=record.position),)),)


class AgentPrompt(Prompting, declared_name=None):
    """Input another agent wrote: the directing session, or a peer session. It never
    starts a turn of its own."""

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return self.absorbed(record, text, session)


class HumanPrompt(Prompting, declared_name='human'):

    def owner_words(self, text: str) -> tuple[str, ...]:
        return (text,)

    def absorbed(self, record: TimedRecord, text: str,
                 session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.finished(
            record.uuid, (UserTranscript(text, timestamp=record.timestamp, position=record.position),))


class TaskNotification(PromptOrigin, declared_name='task-notification'):
    """Its specialized user record owns task-envelope decoding; queued into a running
    turn, the envelope arrives as the prompt."""
    kind: Literal['task-notification']

    def absorbed(self, record: TimedRecord, text: str,
                 session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return TaskEnvelope.from_text(text).updates(record, session.turn.source)

    def delivered(self, text: str) -> tuple[MessageReference, ...]:
        return TaskEnvelope.from_text(text).delivered()


class PeerMessage(AgentPrompt, declared_name='peer'):
    pass


class CoordinatorMessage(AgentPrompt, declared_name='coordinator'):
    """The session that started this subagent, writing to it while it works."""


class AutoContinuation(PromptOrigin, declared_name='auto-continuation'):
    pass


class ToolSupplied(Prompting, declared_name=None):
    """Text Claude records without a sender: a subagent's task, or a local command's echo.
    It starts a turn; the live view does not show it as the person's prompt."""


class Originated(WireModel):
    """Who wrote a prompt, whether it opened a user record or was queued into a running turn.
    Its origin answers what the prompt's text means, so each member says only where the text is."""
    origin: Declared[PromptOrigin] = Field(default_factory=ToolSupplied)

    @abstractmethod
    def prompt_text(self) -> str: ...

    def owner_words(self) -> tuple[str, ...]:
        return self.origin.owner_words(self.prompt_text())

    def delivered(self) -> tuple[MessageReference, ...]:
        return self.origin.delivered(self.prompt_text())


class UserBlock(TaggedWire, ABC, affix=""):

    def ends_turn(self) -> bool:
        return False

    def tally(self, receipts: SessionReceipts) -> None:
        """A prompt's words are not the session's."""

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (TurnStarted(),)

    def prompt_text(self) -> str:
        return ''

    def subagent_progress(self, record: TimedRecord) -> tuple[SubagentProgress, ...]:
        """A tool result or an image decides nothing: the call before it does."""
        return ()


class UserText(TextWire, UserBlock, TextContent, declared_name='text'):

    def prompt_text(self) -> str:
        return self.text

    def ends_turn(self) -> bool:
        return self.text.startswith('[Request interrupted by user')

    def subagent_progress(self, record: TimedRecord) -> tuple[SubagentProgress, ...]:
        """A prompt owes the agent's next call; an interruption ended its turn."""
        return (AnsweredSubagent(),) if self.ends_turn() else (PromptedSubagent(record.timestamp),)

    def updates(self, record: UserRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        if self.ends_turn():
            return session.retired(AllTurnsCancelled())
        return record.origin.prompt(record, self.text, session)


class UserImage(UserBlock, ImageContent, declared_name='image'):
    pass


class ContentBlocks(WireModel):
    """Claude's string shorthand is decoded once for both content families."""

    @field_validator('content', mode='before', check_fields=False)
    @classmethod
    def blocks(cls, value: str | list) -> list:
        return [{'type': 'text', 'text': value}] if isinstance(value, str) else value


class TaskEnvelope(WireModel, ABC):
    @classmethod
    def from_text(cls, text: str) -> TaskEnvelope:
        if not text.startswith('<task-notification>'):
            return ForeignTaskNotification(text=text)
        # Claude Code appends sibling blocks (system-reminder) after the envelope in the same record.
        envelope, closing, _ = text.partition('</task-notification>')
        try:
            root = ElementTree.fromstring(envelope + closing)
        except ElementTree.ParseError:
            # A transcript replays its window on every re-arm, so raising here would stop the
            # session's chat and activity for good over one record.
            return ForeignTaskNotification(text=text)
        event = root.find('event')
        if event is None:
            return ForeignTaskNotification(text=text)
        return MonitorTaskNotification(frames=ClaudeMonitorFrame.decode_event(''.join(event.itertext())))

    @abstractmethod
    def updates(self, record: TimedRecord, source_turn: SourceTurn) -> tuple[RuntimeUpdate, ...]: ...

    def delivered(self) -> tuple[MessageReference, ...]:
        """The bus messages this notification put in front of the model."""
        return ()


class ForeignTaskNotification(TaskEnvelope):
    text: str

    def updates(self, record: TimedRecord, source_turn: SourceTurn) -> tuple[RuntimeUpdate, ...]:
        return ()


@dataclass(frozen=True)
class ReadResultObserved(ConfirmedDelivery):
    """The text a successful Read showed the model. Only the bridge that exported the file
    decides whether it was a message; any other read confirms nothing."""

    path: str
    text: str
    record: ObservedMonitor
    reading: frozenset[str]
    """Paths other Reads in this session had open when this one returned."""
    layout: ContextRows = field(default_factory=ContextRows, kw_only=True)

    def closing(self, export: MonitorReadFile) -> tuple[MonitorReadFile, ...]:
        """The export this Read may retire: none while another Read of it is still open."""
        return () if export.path in self.reading else (export,)

    def rows(self) -> tuple[tuple[int, str], ...]:
        """The lines the reading tool showed, in the layout that tool declares: Read numbers
        them, cat prints them as they are."""
        return self.layout.rows(self.text)

    async def confirmed_by(self, bridge: TranscriptConfirmsDelivery) -> ConfirmedDelivery:
        return replace(self, references=await bridge.read_export(self))


@dataclass(frozen=True)
class FailedReadObserved(ReadResultObserved):
    """A failed Read's text is its error, never file lines: it confirms no part, and only
    releases the export once no other Read of it is open."""

    def rows(self) -> tuple[tuple[int, str], ...]:
        return ()


class MonitorTaskNotification(TaskEnvelope):
    frames: tuple[MonitorFrame, ...]

    def delivered(self) -> tuple[MessageReference, ...]:
        return tuple(reference for frame in self.frames for reference in frame.delivered())

    def updates(self, record: TimedRecord, source_turn: SourceTurn) -> tuple[RuntimeUpdate, ...]:
        return tuple(MonitorObservation(frame, ObservedMonitor(record.uuid, record.timestamp),
                                        source_turn=source_turn)
                     for frame in self.frames)


class TaskContent(WireModel):
    """Both external task-record shapes share envelope decoding and observations."""
    text: TaskEnvelope

    @field_validator('text', mode='before')
    @classmethod
    def envelope(cls, value: str) -> TaskEnvelope:
        return TaskEnvelope.from_text(value)

    def updates(self, record: TimedRecord, source_turn: SourceTurn) -> tuple[RuntimeUpdate, ...]:
        return self.text.updates(record, source_turn)

    def delivered(self) -> tuple[MessageReference, ...]:
        return self.text.delivered()


class TaskText(TaskContent):
    type: Literal['text'] = 'text'


class TaskMessage(ContentBlocks):
    content: list[TaskText]

    def updates(self, record: TimedRecord, source_turn: SourceTurn) -> tuple[RuntimeUpdate, ...]:
        return tuple(update for block in self.content for update in block.updates(record, source_turn))

    def delivered(self) -> tuple[MessageReference, ...]:
        return tuple(reference for block in self.content for reference in block.delivered())


class ToolResult(UserBlock, ContentBlocks, declared_name='tool_result'):
    tool_use_id: ToolCallId
    content: list[Declared[ResultPart]]
    is_error: bool = False

    def tally(self, receipts: SessionReceipts) -> None:
        receipts.resulted(str(self.tool_use_id), self.is_error)

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.tool_result(
            self.tool_use_id, '\n'.join(part.result_text() for part in self.content),
            not self.is_error, record)


class UserMessage(ContentBlocks):
    content: list[Declared[UserBlock]]

    def text(self) -> str:
        return '\n'.join(text for block in self.content if (text := block.prompt_text()))


class PromptRecord(ClaudeRecord, TimedRecord, ABC, declared_name=None):
    """The first prompt after retirement owns the turn; promptId may be reused."""

    promptId: NativeTurnId

    def opening_position(self, following: int) -> int:
        return self.position

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        session.open_turn(self)
        return self.opened_updates(session)

    @abstractmethod
    def opened_updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]: ...


class UserRecord(Originated, PromptRecord, declared_name='user'):
    message: UserMessage

    def ends_turn(self) -> bool:
        return any(block.ends_turn() for block in self.message.content)

    def opened_updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return tuple(update for block in self.message.content for update in block.updates(self, session))

    def request(self) -> str:
        return self.origin.request(self.message.text())

    def prompt_text(self) -> str:
        return self.message.text()

    def tally(self, receipts: SessionReceipts) -> None:
        for block in self.message.content:
            block.tally(receipts)

    def subagent_progress(self) -> tuple[SubagentProgress, ...]:
        return tuple(progress for block in reversed(self.message.content)
                     for progress in block.subagent_progress(self))


class CompactSummary(UserRecord, declared_name='compact-summary'):
    """Claude's summary of the conversation before its latest compaction."""

    def opened_updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (TurnStarted(), Summarized(self.message.text()))

    def request(self) -> str:
        return ''


class TaskUserRecord(PromptRecord, declared_name='task-user'):
    origin: TaskNotification
    message: TaskMessage

    def opened_updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return self.message.updates(self, session.turn.source)

    def delivered(self) -> tuple[MessageReference, ...]:
        return self.message.delivered()


class SubtypeHeader(WireHeader):
    type: str = Field(alias='subtype')


class SystemEvent(TaggedWire, ABC, affix=""):
    header = SubtypeHeader

    @classmethod
    def undeclared_tag(cls, tag: str) -> str:
        """Claude Code extends this family without telling us, so a subtype this build does
        not declare reads as the unmodelled notice rather than refusing the record."""
        logging.info('Claude Code wrote system subtype %r, which this build does not model', tag)
        return UnmodelledSystemEvent.declared_name

    def ends_turn(self) -> bool:
        return False

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def begins_history(self) -> bool:
        return False

    def compacted_at(self) -> tuple[float, ...]:
        return ()


class BridgeStatus(SystemEvent, declared_name='bridge_status'):
    """The remote-control banner and link, not a conversation or turn update."""

    content: str
    url: str


class TurnDuration(SystemEvent, declared_name='turn_duration'):

    def ends_turn(self) -> bool:
        return True

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.retired(AllTurnsEnded())


class CompactBoundary(SystemEvent, TimedRecord, declared_name='compact_boundary'):
    """Claude compacts inside a turn or between two, so the boundary opens the turn its notice
    belongs to, exactly as a prompt does; the summary record after it joins that turn."""

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        session.open_turn(self)
        return session.finished(self.uuid, (NoticeTranscript('Context compacted', timestamp=self.timestamp, position=self.position),))

    def begins_history(self) -> bool:
        return True

    def compacted_at(self) -> tuple[float, ...]:
        return (self.timestamp,)


class StopHookSummary(SystemEvent, declared_name='stop_hook_summary'):
    """A cloud session writes no turn_duration (0 against 17 of these in claude-remote-d), so this
    is the only end of its turn. A hook with output usually continues the turn: in one local
    session, 73 of 121 such summaries were followed by more of the assistant, and 2 of 109 without
    output were."""

    has_output: bool = Field(default=True, alias='hasOutput')

    def ends_turn(self) -> bool:
        return not self.has_output

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.retired(AllTurnsEnded()) if self.ends_turn() else ()


class Informational(SystemEvent, declared_name='informational'):
    pass


class ScheduledTaskFire(SystemEvent, declared_name='scheduled_task_fire'):
    pass


class LocalCommand(SystemEvent, declared_name='local_command'):
    pass


class AwaySummary(SystemEvent, declared_name='away_summary'):
    pass


class AgentsKilled(SystemEvent, declared_name='agents_killed'):
    pass


class ModelRefusalNoFallback(SystemEvent, declared_name='model_refusal_no_fallback'):
    pass


class ModelRefusalFallback(SystemEvent, declared_name='model_refusal_fallback'):
    """Claude Code's notice that a model's safeguards refused a request and it is retrying on
    the fallback model. It delivers nothing and ends no turn; undeclared, its first record
    stopped claude-laptop's bridge on 2026-11-08, and every restart died on the same line."""


class ApiError(SystemEvent, declared_name='api_error'):
    """Claude Code's notice that it is retrying a failed API request (a 529 overload, say). It
    delivers nothing and ends no turn; undeclared, its first record stopped claude-b-ui's watch
    on 2026-11-06 at 19:53Z."""


class UnmodelledSystemEvent(SystemEvent, declared_name='unmodelled'):
    """A notice from a newer Claude Code than this build models.

    Claude Code owns this vocabulary and adds to it whenever it likes, and twice in two days
    an undeclared subtype took a whole session's delivery down rather than costing one unread
    notice: api_error stopped claude-b-ui's watch on 2026-11-06, and model_refusal_fallback
    stopped this bridge on 2026-11-08 on every single reconnect. A notice nobody acts on must
    not be able to do that, so SystemEvent answers TaggedWire.undeclared_tag with this member.

    It inherits every default: delivers nothing, ends no turn, begins no history. A subtype we
    do want behaviour from is still declared on its own, and until someone declares it this
    one keeps the session alive instead of waiting for a release.
    """


class SystemRecord(ClaudeRecord, declared_name='system'):
    subtype: Declared[SystemEvent]

    @model_validator(mode='before')
    @classmethod
    def decode_subtype(cls, value: dict) -> dict:
        # Claude places both discriminators on one envelope. Decode that shape
        # once; its declared subtype owns the behavior and any retained fields.
        return {**value, 'subtype': value}

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return self.subtype.updates(session)

    def begins_history(self) -> bool:
        return self.subtype.begins_history()

    def compacted_at(self) -> tuple[float, ...]:
        return self.subtype.compacted_at()

    def ends_turn(self) -> bool:
        return self.subtype.ends_turn()


class MetadataRecord(ClaudeRecord, ABC, declared_name=None):
    pass


class AttachmentPayload(OpenWire, ABC, affix=""):

    @abstractmethod
    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]: ...

    def owner_words(self) -> tuple[str, ...]:
        """What the person typed in this attachment; only a queued prompt carries any."""
        return ()

    def delivered(self) -> tuple[MessageReference, ...]:
        """The bus messages this attachment carried; only a queued prompt carries any."""
        return ()


class QueuedCommand(Originated, AttachmentPayload, declared_name='queued_command'):
    """A prompt sent while a turn runs: its origin decides what the folded-in text is."""
    prompt: UserMessage

    @field_validator('prompt', mode='before')
    @classmethod
    def message(cls, value: str | list) -> dict:
        # Claude stores the queued prompt as a bare user message content.
        return {'content': value}

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return self.origin.absorbed(record, self.prompt.text(), session)

    def prompt_text(self) -> str:
        return self.prompt.text()


class ForeignAttachment(AttachmentPayload, declared_name='foreign'):

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()


class Attachment(ClaudeRecord, TimedRecord, declared_name='attachment'):
    attachment: Declared[AttachmentPayload]

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return self.attachment.updates(self, session)

    def owner_words(self) -> tuple[str, ...]:
        return self.attachment.owner_words()

    def delivered(self) -> tuple[MessageReference, ...]:
        return self.attachment.delivered()


class QueueOperation(MetadataRecord, declared_name='queue-operation'):
    pass


class Mode(MetadataRecord, declared_name='mode'):
    pass


class PermissionMode(MetadataRecord, declared_name='permission-mode'):
    pass


class AtisLatch(MetadataRecord, declared_name='atis-latch'):
    pass


class HistorySuppression(MetadataRecord, declared_name='history-suppression'):
    pass


class LastPrompt(MetadataRecord, declared_name='last-prompt'):
    pass


class AiTitle(MetadataRecord, declared_name='ai-title'):
    pass


class ToolsUsed(MetadataRecord, declared_name='tools-used'):
    """The tools the conversation had used, recorded at a compaction; nothing here reaches the bus."""


class PrLink(MetadataRecord, declared_name='pr-link'):
    pass


class Relocated(MetadataRecord, declared_name='relocated'):
    """The session moved to another working directory, as entering a worktree does."""


class WorktreeState(MetadataRecord, declared_name='worktree-state'):
    """The worktree the session entered; nothing here reaches the bus."""


class FileHistorySnapshot(MetadataRecord, declared_name='file-history-snapshot'):
    pass


class FileHistoryDelta(MetadataRecord, declared_name='file-history-delta'):
    pass


class CostState(MetadataRecord, declared_name='cost-state'):
    pass


class ApiRequest(MetadataRecord, declared_name='api-request'):
    """The parameters of one model request the harness sent."""


class OfferedClaudeTool(WireModel):
    name: str


class ClaudeRequestShape(WireModel):
    tools: list[OfferedClaudeTool] | None = None

    def offered(self, previous: OfferedTools) -> tuple[OfferedTools, ...]:
        if self.tools is None:
            return previous.without_catalogue()
        return previous.on_catalogue(frozenset(ToolName(tool.name) for tool in self.tools))


class ApiRequestShape(MetadataRecord, declared_name='api-request-shape'):
    """The system prompt and tools a request was sent with, stored once by hash."""

    shape: ClaudeRequestShape
    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return tuple(update for offered in self.shape.offered(session.offered_tools)
                     for update in session.offered(offered))


class ApiRequestBlob(MetadataRecord, declared_name='api-request-blob'):
    """One message a request carried, stored once by hash."""


class LocalCommandOutput(MetadataRecord, declared_name='local-command'):
    """A terminal-only slash command and its output."""


class HarnessNote(MetadataRecord, declared_name='harness-note'):
    """Text the harness adds for the model without a sender: reminders, skill bodies, image notes."""


class ForkContextReference(MetadataRecord, declared_name='fork-context-ref'):
    """A forked subagent starts from its parent's context, stored in the parent's transcript."""
    parentSessionId: str

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (HistoryNotice(f'Context inherited from session {self.parentSessionId} is not imported.'),)


class CustomTitle(MetadataRecord, declared_name='custom-title'):
    """The name a person gave the session."""
    customTitle: str

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (SessionFacts(title=ThreadText.external(self.customTitle)),)


class BridgeSession(MetadataRecord, declared_name='bridge-session'):
    """The session was opened to claude.ai by /remote-control; nothing here reaches the bus."""


class SessionOwner(MetadataRecord, declared_name='session-owner'):
    """The claude.ai account and organization that own the session; nothing here reaches the bus."""


class AgentName(MetadataRecord, declared_name='agent-name'):
    """The name the session runs under as an agent."""
    agentName: str

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (SessionFacts(title=ThreadText.external(self.agentName)),)


class ContinuedIn(MetadataRecord, declared_name='continued-in'):
    """The conversation went on in another session."""
    continuedInSessionId: str

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (HistoryNotice(f'The conversation continued in session {self.continuedInSessionId}.'),)


CLAUDE_RECORD = WireModel.adapter(ClaudeRecord.declared_union())
