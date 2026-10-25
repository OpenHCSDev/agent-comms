"""Claude's completed JSONL blocks own transcript items and activity."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Literal, Self
from xml.etree import ElementTree

from pydantic import (
    Field,
    JsonValue,
    ModelWrapValidatorHandler,
    field_validator,
    model_validator,
)

from agent_comms.agent_events import TranscriptModelUsage
from agent_comms.cli_protocol import TaggedWire, TextWire, TimestampedWire, WireHeader
from agent_comms.field_codec import FieldCodec, MalformedRecord, MessageId
from agent_comms.pi_counts import PiCount, PiCountWire
from agent_comms.recorded_time import EventTime
from agent_comms.runtime_updates import (
    ActiveItem,
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
    TurnStarted,
)
from agent_comms.thread_identity import OfferedTools, ToolName, UnobservedTools
from agent_comms.thread_model import ThreadModel
from agent_comms.thread_text import ThreadText
from agent_comms.tool_calls import CliToolArguments, ToolCall
from agent_comms.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    TranscriptEvent,
    UserTranscript,
)
from agent_comms.turn_item import NativeTurnId
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase, TurnPhase

from ..wire_models import WireModel
from .shared import (
    MONITOR_FRAME,
    ForeignMonitorFrame,
    MonitorFrame,
    MonitorIncomingFrame,
    MonitorObservation,
    MonitorPart,
    MonitorProtocolFrame,
    ObservedMonitor,
)


class ClaudeMonitorFrame(MonitorIncomingFrame):
    """Claude Monitor caps lines at 500 and joined batches at 3000 UTF-16 units."""
    EVENT_CHARS = 500
    BATCH_CHARS = 3000
    HEADER = 'Comms in'
    JSON_TEXT = WireModel.adapter(str)

    @classmethod
    def event_size(cls, text: str) -> int:
        return len(json.dumps(text, ensure_ascii=False).encode('utf-16-le')) // 2

    def header(self, number: int, total: int) -> str:
        return (f'{self.HEADER} · {self.message.sender} · '
                f'{self.message.message_id} · {number}/{total}\n')

    @property
    def parts(self) -> tuple[str, ...]:
        # At least one body character fits each part. Using the maximum possible
        # ordinal width avoids rebuilding the body to discover its final count.
        upper = len(self.message.body)  # Message already requires a nonempty body.
        budget = self.EVENT_CHARS - self.event_size(self.header(upper, upper))
        bodies: list[str] = []
        current: list[str] = []
        used = 0
        for character in self.message.body:
            size = self.event_size(character) - 2  # JSON quotes belong to the header budget.
            if size > budget:
                raise ValueError('Claude Monitor header leaves no room for a body character')
            if used + size > budget:
                bodies.append(''.join(current))
                current, used = [], 0
            current.append(character)
            used += size
        bodies.append(''.join(current))
        total = len(bodies)
        return tuple(self.header(number, total) + body
                     for number, body in enumerate(bodies, 1))

    @classmethod
    def decode_line(cls, line: str) -> MonitorFrame:
        if line.startswith(MonitorProtocolFrame.wire_prefix):
            return MONITOR_FRAME.validate_json(line)
        # Classify the external header before decoding its JSON string. Old or
        # unrelated monitor text, including truncated text, is a named foreign frame.
        if not line.startswith('"' + cls.HEADER + ' · '):
            return ForeignMonitorFrame(text=line)
        raw_header, separator, _ = line.partition(r'\n')
        if not separator or not line.endswith('"'):
            return ForeignMonitorFrame(text=line)
        columns = raw_header[1:].rsplit(' · ', 2)
        if len(columns) != 3:
            return ForeignMonitorFrame(text=line)
        number, slash, total = columns[-1].partition('/')
        if not slash or not number.isdecimal() or not total.isdecimal():
            return ForeignMonitorFrame(text=line)
        decoded = cls.JSON_TEXT.validate_json(line)
        header, _, body = decoded.partition('\n')
        sender, message_id, _ = header.removeprefix(cls.HEADER + ' · ').rsplit(' · ', 2)
        try:
            identity = FieldCodec.decode(MessageId, message_id)
        except MalformedRecord:
            return ForeignMonitorFrame(text=line)
        return MonitorPart(id=identity, sender=sender, number=int(number),
                           total=int(total), body=body)

    @classmethod
    def decode_event(cls, event: str) -> tuple[MonitorFrame, ...]:
        return tuple(cls.decode_line(line) for line in event.splitlines())


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


class ClaudeTurn(ABC):
    """The transcript's identified turn owns item identities."""

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

    def started(self, tool: ClaudeToolDraft,
                phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]:
        return (StartedItem(self.id, tool.call.id,
                            ToolRunningPhase(tools=((tool.call.id, tool.call.title),)), tool),)

    def finished(self, item: str, events: tuple[TranscriptEvent, ...],
                 phase: ClaudeReaderPhase) -> tuple[RuntimeUpdate, ...]:
        return (FinishedItem(self.id, item, events),)

    def active(self, tool_id: str, phase: ClaudeReaderPhase,
               turn_phase: TurnPhase) -> tuple[RuntimeUpdate, ...]:
        return (ActiveItem(self.id, tool_id, turn_phase),)


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
                *self.turn.active(tool_id, self.phase, ModelWaitPhase()))

    def finished(self, item: str, events: tuple[TranscriptEvent, ...]) -> tuple[RuntimeUpdate, ...]:
        return self.turn.finished(item, events, self.phase)


class TimedRecord(TimestampedWire):
    position: int = Field(exclude=True)
    uuid: str


class ClaudeUserHeader(WireHeader):
    """Claude's sparse user header; Pydantic owns which fields were supplied."""

    prompt_id: str = Field(default='', alias='promptId')
    origin: KindHeader = Field(default_factory=lambda: KindHeader(kind=''))
    is_compact_summary: bool = Field(default=False, alias='isCompactSummary')
    is_meta: bool = Field(default=False, alias='isMeta')

    def record_type(self) -> str:
        if self.origin.type == TaskNotification.declared_name:
            return TaskUserRecord.declared_name
        if 'prompt_id' not in self.model_fields_set:
            return LocalCommandOutput.declared_name
        if self.is_compact_summary:
            return CompactSummary.declared_name
        if self.is_meta and 'origin' not in self.model_fields_set:
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


class ClaudeRecord(TaggedWire, ABC, affix=""):
    header = ClaudeHeader
    cwd: str = ''
    version: str | None = None

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def begins_history(self) -> bool:
        """Whether Claude replaced everything before this record (compaction)."""
        return False

    def request(self) -> str:
        """The prompt this record carries, read without the session around it."""
        return ''


class AssistantBlock(TaggedWire, ABC, affix=""):

    @abstractmethod
    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]: ...


class TextContent(WireModel):
    text: str

    def result_text(self) -> str:
        return self.text


class ImageContent(WireModel):
    def result_text(self) -> str:
        return '[image]'


class AssistantText(TextWire, AssistantBlock, TextContent, declared_name='text'):

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.finished(record.uuid, (AssistantTranscript(self.text, timestamp=record.timestamp, position=record.position),))


class RedactedThinking(AssistantBlock, declared_name='redacted_thinking'):

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.turn.active(record.uuid, session.phase, ModelWaitPhase())


class NarrationBlock(RedactedThinking, declared_name='thinking'):
    """Claude Code saves its reasoning under this tag with empty text, and the
    narration it shows before a tool call under the same tag with the text
    filled in. The text is a message to the reader, never hidden reasoning."""

    thinking: str

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        activity = super().updates(record, session)
        if not self.thinking:
            return activity
        return (*activity, *session.finished(
            record.uuid, (AssistantTranscript(self.thinking, timestamp=record.timestamp, position=record.position),)))


class ToolUse(AssistantBlock, declared_name='tool_use'):
    id: str
    name: str
    input: dict[str, JsonValue]

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        call = self.call()
        return session.started(ClaudeToolDraft(
            EventTime.capture(record.timestamp),
            (call.started(record.position, record.timestamp),), call))


    def call(self) -> ToolCall:
        return CliToolArguments.decode(ToolCall(self.id, self.name, self.input), self.input)


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
    content: list[AssistantBlock.declared_union()]
    model: str = ''
    usage: ClaudeUsage = Field(default_factory=ClaudeUsage)


class AssistantRecord(ClaudeRecord, TimedRecord, declared_name='assistant'):
    message: AssistantMessage

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (*(update for block in self.message.content for update in block.updates(self, session)),
                *session.answered(self.message))


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
    """Who wrote a user record. Only a person's prompt is shown live as input;
    another agent's message is conversation history the live view hides."""
    header = KindHeader

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def request(self, text: str) -> str:
        return ''


class Prompting(PromptOrigin, ABC, declared_name=None):
    """An origin whose text is a request to the model."""

    def request(self, text: str) -> str:
        return text


class AgentPrompt(Prompting, declared_name=None):
    """Input another agent wrote: the commanding session, or a peer session."""

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (HiddenItem((UserTranscript(text, timestamp=record.timestamp, position=record.position),)),)


class HumanPrompt(Prompting, declared_name='human'):

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (TurnStarted(), *session.finished(
            record.uuid, (UserTranscript(text, timestamp=record.timestamp, position=record.position),)))


class TaskNotification(PromptOrigin, declared_name='task-notification'):
    """Its specialized user record owns task-envelope decoding."""
    kind: Literal['task-notification']


class PeerMessage(AgentPrompt, declared_name='peer'):
    pass


class CoordinatorMessage(AgentPrompt, declared_name='coordinator'):
    """The session that started this subagent, writing to it while it works."""


class AutoContinuation(PromptOrigin, declared_name='auto-continuation'):
    pass


class ToolSupplied(Prompting, declared_name=None):
    """Text Claude records without a sender: a subagent's task, or a local command's echo.
    It starts a turn; the live view does not show it as the person's prompt."""

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (TurnStarted(), HiddenItem((UserTranscript(text, timestamp=record.timestamp, position=record.position),)))


class UserBlock(TaggedWire, ABC, affix=""):

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (TurnStarted(),)

    def prompt_text(self) -> str:
        return ''


class UserText(TextWire, UserBlock, TextContent, declared_name='text'):

    def prompt_text(self) -> str:
        return self.text

    def updates(self, record: UserRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        if self.text.startswith('[Request interrupted by user'):
            return (AllTurnsEnded(),)
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
        root = ElementTree.fromstring(envelope + closing)  # Malformed envelope fails at ingress.
        event = root.find('event')
        if event is None:
            return ForeignTaskNotification(text=text)
        return MonitorTaskNotification(frames=ClaudeMonitorFrame.decode_event(''.join(event.itertext())))

    @abstractmethod
    def updates(self, record: TimedRecord) -> tuple[RuntimeUpdate, ...]: ...


class ForeignTaskNotification(TaskEnvelope):
    text: str

    def updates(self, record: TimedRecord) -> tuple[RuntimeUpdate, ...]:
        return ()


class MonitorTaskNotification(TaskEnvelope):
    frames: tuple[MonitorFrame, ...]

    def updates(self, record: TimedRecord) -> tuple[RuntimeUpdate, ...]:
        return tuple(MonitorObservation(frame,
                                        ObservedMonitor(record.uuid, record.timestamp))
                     for frame in self.frames)


class TaskContent(WireModel):
    """Both external task-record shapes share envelope decoding and observations."""
    text: TaskEnvelope

    @field_validator('text', mode='before')
    @classmethod
    def envelope(cls, value: str) -> TaskEnvelope:
        return TaskEnvelope.from_text(value)

    def updates(self, record: TimedRecord) -> tuple[RuntimeUpdate, ...]:
        return self.text.updates(record)


class TaskText(TaskContent):
    type: Literal['text'] = 'text'


class TaskMessage(ContentBlocks):
    content: list[TaskText]

    def updates(self, record: TimedRecord) -> tuple[RuntimeUpdate, ...]:
        return tuple(update for block in self.content for update in block.updates(record))


class ToolResult(UserBlock, ContentBlocks, declared_name='tool_result'):
    tool_use_id: str
    content: list[ResultPart.declared_union()]
    is_error: bool = False

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.tool_result(
            self.tool_use_id, '\n'.join(part.result_text() for part in self.content),
            not self.is_error, record)


class UserMessage(ContentBlocks):
    content: list[UserBlock.declared_union()]


class UserRecord(ClaudeRecord, TimedRecord, declared_name='user'):
    promptId: str
    message: UserMessage
    origin: PromptOrigin.declared_union() = Field(default_factory=ToolSupplied)

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        session.turn = KnownTurn(FieldCodec.decode(NativeTurnId, self.promptId))
        return tuple(update for block in self.message.content for update in block.updates(self, session))

    @property
    def text(self) -> str:
        return '\n'.join(text for block in self.message.content if (text := block.prompt_text()))

    def request(self) -> str:
        return self.origin.request(self.text)


class CompactSummary(UserRecord, declared_name='compact-summary'):
    """Claude's summary of the conversation before its latest compaction."""

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        session.turn = KnownTurn(FieldCodec.decode(NativeTurnId, self.promptId))
        return (TurnStarted(), Summarized(self.text))

    def request(self) -> str:
        return ''


class TaskUserRecord(ClaudeRecord, TimedRecord, declared_name='task-user'):
    origin: TaskNotification
    message: TaskMessage

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return self.message.updates(self)


class SubtypeHeader(WireHeader):
    type: str = Field(alias='subtype')


class SystemEvent(TaggedWire, ABC, affix=""):
    header = SubtypeHeader

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return ()

    def begins_history(self) -> bool:
        return False


class TurnDuration(SystemEvent, declared_name='turn_duration'):

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return (AllTurnsEnded(),)


class CompactBoundary(SystemEvent, TimedRecord, declared_name='compact_boundary'):

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return session.finished(self.uuid, (NoticeTranscript('Context compacted', timestamp=self.timestamp, position=self.position),))

    def begins_history(self) -> bool:
        return True


class StopHookSummary(SystemEvent, declared_name='stop_hook_summary'):
    pass


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


class SystemRecord(ClaudeRecord, declared_name='system'):
    subtype: SystemEvent.declared_union()

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


class MetadataRecord(ClaudeRecord, ABC, declared_name=None):
    pass


class AttachmentHeader(WireHeader):
    @model_validator(mode='before')
    @classmethod
    def task_notification(cls, value: dict) -> dict:
        if (value.get('type') == QueuedTaskNotification.declared_name
                and value.get('origin', {}).get('kind') == TaskNotification.declared_name):
            return value
        return {'type': ForeignAttachment.declared_name}


class AttachmentPayload(TaggedWire, ABC, affix=""):
    header = AttachmentHeader

    @abstractmethod
    def updates(self, record: TimedRecord) -> tuple[RuntimeUpdate, ...]: ...


class QueuedTaskNotification(TaskContent, AttachmentPayload, declared_name='queued_command'):
    text: TaskEnvelope = Field(alias='prompt')
    origin: TaskNotification


class ForeignAttachment(AttachmentPayload, declared_name='foreign'):

    def updates(self, record: TimedRecord) -> tuple[RuntimeUpdate, ...]:
        return ()


class Attachment(ClaudeRecord, TimedRecord, declared_name='attachment'):
    attachment: AttachmentPayload.declared_union()

    def updates(self, session: ClaudeSession) -> tuple[RuntimeUpdate, ...]:
        return self.attachment.updates(self)


class QueueOperation(MetadataRecord, declared_name='queue-operation'):
    pass


class Mode(MetadataRecord, declared_name='mode'):
    pass


class PermissionMode(MetadataRecord, declared_name='permission-mode'):
    pass


class AtisLatch(MetadataRecord, declared_name='atis-latch'):
    pass


class LastPrompt(MetadataRecord, declared_name='last-prompt'):
    pass


class AiTitle(MetadataRecord, declared_name='ai-title'):
    pass


class PrLink(MetadataRecord, declared_name='pr-link'):
    pass


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
