"""Claude's completed JSONL blocks own transcript items and activity."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import ClassVar, Literal
from xml.etree import ElementTree

from metaclass_registry import RegistryConfig
from pydantic import Field, JsonValue, TypeAdapter, ValidationError, field_validator, model_validator

from agent_comms.activity import ActivityState
from agent_comms.field_codec import FieldCodec
from agent_comms.native_tools import BashTool, ReadTool, EditTool, WriteTool, GrepTool, GlobTool
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.tool_results import ToolDiff
from agent_comms.cli_protocol import TaggedWire, WireHeader, WireModel
from agent_comms.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    ThinkingTranscript,
    TranscriptEvent,
    UserTranscript,
)

from .shared import (
    ActiveItem,
    FinishedItem,
    HiddenItem,
    HistoryNotice,
    SessionFacts,
    Summarized,
    ToolCall,
    TranscriptUpdate,
    StatusUpdate,
    TurnEnded,
    TurnStarted,
    IncomingFrame,
    MonitorFrame,
    MonitorPart,
    MonitorProbe,
    ForeignMonitorFrame,
    MonitorObservation,
    ObservedMonitor,
    MONITOR_FRAME,
)


class ClaudeMonitorFrame(IncomingFrame):
    """Claude Monitor caps lines at 500 and joined batches at 3000 UTF-16 units."""
    EVENT_CHARS = 500
    BATCH_CHARS = 3000
    HEADER = 'Comms in'
    JSON_TEXT = TypeAdapter(str)

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
        if line.startswith(MonitorProbe.wire_prefix):
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
        return MonitorPart(id=message_id, sender=sender, number=int(number),
                           total=int(total), body=body)

    @classmethod
    def decode_event(cls, event: str) -> tuple[MonitorFrame, ...]:
        return tuple(cls.decode_line(line) for line in event.splitlines())


class ClaudeReaderPhase(ABC):
    """The initial bounded window and live tail have different guarantees."""

    @abstractmethod
    def status(self, update: StatusUpdate) -> tuple[TranscriptUpdate, ...]: ...

    @abstractmethod
    def window_ended(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]: ...

    @abstractmethod
    def missing_tool(self, tool_id: str) -> tuple[TranscriptUpdate, ...]: ...

    @abstractmethod
    def unturned_finish(self) -> tuple[TranscriptUpdate, ...]: ...


@dataclass
class CatchUp(ClaudeReaderPhase):
    pending: tuple[StatusUpdate, ...] = ()

    def status(self, update: StatusUpdate) -> tuple[TranscriptUpdate, ...]:
        self.pending = (update,)
        return ()

    def window_ended(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        session.phase = Live()
        return self.pending

    def missing_tool(self, tool_id: str) -> tuple[TranscriptUpdate, ...]:
        return ()

    def unturned_finish(self) -> tuple[TranscriptUpdate, ...]:
        return ()


class Live(ClaudeReaderPhase):
    def status(self, update: StatusUpdate) -> tuple[TranscriptUpdate, ...]:
        return (update,)

    def window_ended(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return ()

    def missing_tool(self, tool_id: str) -> tuple[TranscriptUpdate, ...]:
        raise ValueError(f'Claude result has no tool start: {tool_id}')

    def unturned_finish(self) -> tuple[TranscriptUpdate, ...]:
        raise ValueError('Claude completed an item before identifying its turn')


class ClaudeTurn(ABC):
    """The transcript's identified turn owns item identities."""

    @abstractmethod
    def finished(self, item: str, events: tuple[TranscriptEvent, ...],
                 phase: ClaudeReaderPhase) -> tuple[TranscriptUpdate, ...]: ...

    @abstractmethod
    def active(self, tool_id: str, phase: ClaudeReaderPhase,
               state: ActivityState, detail: str) -> tuple[TranscriptUpdate, ...]: ...


class UnknownTurn(ClaudeTurn):
    def finished(self, item: str, events: tuple[TranscriptEvent, ...],
                 phase: ClaudeReaderPhase) -> tuple[TranscriptUpdate, ...]:
        return phase.unturned_finish()

    def active(self, tool_id: str, phase: ClaudeReaderPhase,
               state: ActivityState, detail: str) -> tuple[TranscriptUpdate, ...]:
        return phase.unturned_finish()


@dataclass(frozen=True)
class KnownTurn(ClaudeTurn):
    id: str

    def finished(self, item: str, events: tuple[TranscriptEvent, ...],
                 phase: ClaudeReaderPhase) -> tuple[TranscriptUpdate, ...]:
        return (FinishedItem(self.id, item, events),)

    def active(self, tool_id: str, phase: ClaudeReaderPhase,
               state: ActivityState, detail: str) -> tuple[TranscriptUpdate, ...]:
        return (ActiveItem(self.id, tool_id, state, detail),)


@dataclass
class ClaudeSession:
    turn: ClaudeTurn = field(default_factory=UnknownTurn)
    tools: dict[str, ToolCall] = field(default_factory=dict)
    phase: ClaudeReaderPhase = field(default_factory=CatchUp)
    project: str = ''

    def located(self, cwd: str) -> tuple[TranscriptUpdate, ...]:
        if not cwd or cwd == self.project:
            return ()
        self.project = cwd
        return (SessionFacts(project=cwd),)

    def started(self, call: ToolCall) -> None:
        self.tools[call.id] = call

    def tool_result(self, tool_id: str, text: str, ok: bool) -> tuple[TranscriptUpdate, ...]:
        try:
            call = self.tools.pop(tool_id)
        except KeyError:
            return self.phase.missing_tool(tool_id)
        return (*self.finished(tool_id, call.finished(text, ok)),
                *self.turn.active(tool_id, self.phase, ActivityState.WORKING, ''))

    def finished(self, item: str, events: tuple[TranscriptEvent, ...]) -> tuple[TranscriptUpdate, ...]:
        return self.turn.finished(item, events, self.phase)


class TimedRecord(WireModel):
    uuid: str
    timestamp: float

    @field_validator('timestamp', mode='before')
    @classmethod
    def epoch(cls, value: str) -> float:
        return datetime.fromisoformat(value).timestamp()


class ClaudeHeader(WireHeader):
    @model_validator(mode='before')
    @classmethod
    def local_command(cls, value: dict) -> dict:
        # Local slash commands (/login, /cost) are written as user records
        # without a prompt; they never reach the model. Decode that shape here.
        # Harness notes (reminders, skill text) are meta records with no sender;
        # a compaction summary is a user record flagged as one.
        if value.get('type') == 'user':
            if value.get('origin', {}).get('kind') == TaskNotification.wire_tag:
                return {'type': TaskUserRecord.wire_tag}
            if 'promptId' not in value:
                return {'type': LocalCommandOutput.wire_tag}
            if value.get('isCompactSummary'):
                return {'type': CompactSummary.wire_tag}
            if value.get('isMeta') and 'origin' not in value:
                return {'type': HarnessNote.wire_tag}
        return value


class ClaudeRecord(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = ClaudeHeader
    cwd: str = ''

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return ()

    def begins_history(self) -> bool:
        """Whether Claude replaced everything before this record (compaction)."""
        return False


class AssistantBlock(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    @abstractmethod
    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]: ...


class TextContent(WireModel):
    text: str

    def result_text(self) -> str:
        return self.text


class ImageContent(WireModel):
    def result_text(self) -> str:
        return '[image]'


class AssistantText(AssistantBlock, TextContent):
    wire_tag = 'text'

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.finished(record.uuid, (AssistantTranscript(self.text, timestamp=record.timestamp),))


class RedactedThinking(AssistantBlock):
    wire_tag = 'redacted_thinking'

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.turn.active(record.uuid, session.phase, ActivityState.THINKING, '')


class ThinkingBlock(RedactedThinking):
    wire_tag = 'thinking'
    thinking: str

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        activity = super().updates(record, session)
        if not self.thinking:
            return activity
        return (*activity, *session.finished(
            record.uuid, (ThinkingTranscript(self.thinking, timestamp=record.timestamp),)))


class ClaudeToolArguments(WireModel):
    claude_names: ClassVar[tuple[str, ...]] = ()
    declaration: ClassVar[type[ToolDeclaration]]
    types: ClassVar[dict[str, type[ClaudeToolArguments]]] = {}
    model_config = {'extra': 'allow'}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name in cls.claude_names:
            cls.types[name] = cls

    def diff(self) -> ToolDiff | None:
        return None


class ClaudeCommandArguments(ClaudeToolArguments):
    claude_names, declaration = ('Bash',), BashTool
    command: str


class ClaudePatternArguments(ClaudeToolArguments):
    claude_names, declaration = ('Grep',), GrepTool
    pattern: str


class ClaudeGlobArguments(ClaudePatternArguments):
    claude_names, declaration = ('Glob',), GlobTool


class ClaudePathArguments(ClaudeToolArguments):
    claude_names, declaration = ('Read',), ReadTool
    path: str = Field(alias='file_path')


class ClaudeReplacement(WireModel):
    old_string: str
    new_string: str


class ClaudeEditArguments(ClaudePathArguments):
    claude_names, declaration = ('Edit', 'MultiEdit'), EditTool
    edits: list[ClaudeReplacement]

    @model_validator(mode='before')
    @classmethod
    def replacements(cls, value):
        return {key: item for key, item in value.items() if key not in ClaudeReplacement.model_fields} | {
            'edits': value.get('edits', [value])}

    def diff(self) -> ToolDiff:
        return ToolDiff(''.join(patch.text for edit in self.edits
            if (patch := ToolDiff.from_versions(self.path, edit.old_string, edit.new_string)) is not None))


class ClaudeWriteArguments(ClaudePathArguments):
    claude_names, declaration = ('Write',), WriteTool
    content: str

    def diff(self) -> ToolDiff | None:
        return ToolDiff.from_versions(self.path, '', self.content,
                                     before_path=self.path + ' (previous content not supplied)')


class ToolUse(AssistantBlock):
    wire_tag = 'tool_use'
    id: str
    name: str
    input: dict[str, JsonValue]

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        call = self.call()
        session.started(call)
        return session.turn.active(self.id, session.phase, ActivityState.WORKING, call.title)


    def call(self) -> ToolCall:
        """A call to a tool with a declared shape uses that declaration. The model can send input
        that does not fit (Claude refuses it with an error result); it is shown as sent."""
        arguments_type = ClaudeToolArguments.types.get(self.name)
        if arguments_type is not None:
            try:
                arguments = arguments_type.model_validate(self.input)
            except ValidationError:
                pass
            else:
                return ToolCall(self.id, FieldCodec.encode(arguments.declaration), arguments.model_dump(), arguments.diff())
        return ToolCall(self.id, self.name, self.input)


class AssistantMessage(WireModel):
    content: list[AssistantBlock.declared_union()]


class AssistantRecord(ClaudeRecord, TimedRecord):
    wire_tag = 'assistant'
    message: AssistantMessage

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return tuple(update for block in self.message.content for update in block.updates(self, session))


class ResultPart(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    @abstractmethod
    def result_text(self) -> str: ...


class ResultText(TextContent, ResultPart):
    wire_tag = 'text'


class ResultImage(ImageContent, ResultPart):
    wire_tag = 'image'


class ResultToolReference(ResultPart):
    wire_tag = 'tool_reference'
    tool_name: str

    def result_text(self) -> str:
        return f'Tool available: {self.tool_name}'


class KindHeader(WireHeader):
    type: str = Field(alias='kind')


class PromptOrigin(TaggedWire, ABC):
    """Who wrote a user record. Only a person's prompt is shown live as input;
    another agent's message is conversation history the live view hides."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = KindHeader

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return ()


class AgentPrompt(PromptOrigin):
    """Input another agent wrote: the commanding session, or a peer session."""

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (HiddenItem((UserTranscript(text, timestamp=record.timestamp),)),)


class HumanPrompt(PromptOrigin):
    wire_tag = 'human'

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (TurnStarted(), *session.finished(
            record.uuid, (UserTranscript(text, timestamp=record.timestamp),)))


class TaskNotification(PromptOrigin):
    """Its specialized user record owns task-envelope decoding."""
    wire_tag = 'task-notification'
    kind: Literal['task-notification']


class PeerMessage(AgentPrompt):
    wire_tag = 'peer'


class CoordinatorMessage(AgentPrompt):
    """The session that started this subagent, writing to it while it works."""
    wire_tag = 'coordinator'


class AutoContinuation(PromptOrigin):
    wire_tag = 'auto-continuation'


class ToolSupplied(PromptOrigin):
    """Text Claude records without a sender: a subagent's task, or a local command's echo.
    It starts a turn; the live view does not show it as the person's prompt."""

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (TurnStarted(), HiddenItem((UserTranscript(text, timestamp=record.timestamp),)))


class UserBlock(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (TurnStarted(),)


class UserText(UserBlock, TextContent):
    wire_tag = 'text'

    def updates(self, record: UserRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        if self.text.startswith('[Request interrupted by user'):
            return (TurnEnded(),)
        return record.origin.prompt(record, self.text, session)


class UserImage(UserBlock, ImageContent):
    wire_tag = 'image'


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
    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]: ...


class ForeignTaskNotification(TaskEnvelope):
    text: str

    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]:
        return ()


class MonitorTaskNotification(TaskEnvelope):
    frames: tuple[MonitorFrame, ...]

    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]:
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

    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]:
        return self.text.updates(record)


class TaskText(TaskContent):
    type: Literal['text'] = 'text'


class TaskMessage(ContentBlocks):
    content: list[TaskText]

    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]:
        return tuple(update for block in self.content for update in block.updates(record))


class ToolResult(UserBlock, ContentBlocks):
    wire_tag = 'tool_result'
    tool_use_id: str
    content: list[ResultPart.declared_union()]
    is_error: bool = False

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.tool_result(self.tool_use_id, '\n'.join(part.result_text() for part in self.content), not self.is_error)


class UserMessage(ContentBlocks):
    content: list[UserBlock.declared_union()]


class UserRecord(ClaudeRecord, TimedRecord):
    wire_tag = 'user'
    promptId: str
    message: UserMessage
    origin: PromptOrigin.declared_union() = Field(default_factory=ToolSupplied)

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        session.turn = KnownTurn(self.promptId)
        return tuple(update for block in self.message.content for update in block.updates(self, session))


class CompactSummary(UserRecord):
    """Claude's summary of the conversation before its latest compaction."""
    wire_tag = 'compact-summary'

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        session.turn = KnownTurn(self.promptId)
        return (TurnStarted(), Summarized('\n'.join(
            block.text for block in self.message.content if isinstance(block, UserText))))


class TaskUserRecord(ClaudeRecord, TimedRecord):
    wire_tag = 'task-user'
    origin: TaskNotification
    message: TaskMessage

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return self.message.updates(self)


class SubtypeHeader(WireHeader):
    type: str = Field(alias='subtype')


class SystemEvent(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = SubtypeHeader

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return ()

    def begins_history(self) -> bool:
        return False


class TurnDuration(SystemEvent):
    wire_tag = 'turn_duration'

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (TurnEnded(),)


class CompactBoundary(SystemEvent, TimedRecord):
    wire_tag = 'compact_boundary'

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.finished(self.uuid, (NoticeTranscript('Context compacted', timestamp=self.timestamp),))

    def begins_history(self) -> bool:
        return True


class StopHookSummary(SystemEvent):
    wire_tag = 'stop_hook_summary'


class Informational(SystemEvent):
    wire_tag = 'informational'


class ScheduledTaskFire(SystemEvent):
    wire_tag = 'scheduled_task_fire'


class LocalCommand(SystemEvent):
    wire_tag = 'local_command'


class AwaySummary(SystemEvent):
    wire_tag = 'away_summary'


class AgentsKilled(SystemEvent):
    wire_tag = 'agents_killed'


class ModelRefusalNoFallback(SystemEvent):
    wire_tag = 'model_refusal_no_fallback'


class SystemRecord(ClaudeRecord):
    wire_tag = 'system'
    subtype: SystemEvent.declared_union()

    @model_validator(mode='before')
    @classmethod
    def decode_subtype(cls, value: dict) -> dict:
        # Claude places both discriminators on one envelope. Decode that shape
        # once; its declared subtype owns the behavior and any retained fields.
        return {'subtype': value}

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return self.subtype.updates(session)

    def begins_history(self) -> bool:
        return self.subtype.begins_history()


class MetadataRecord(ClaudeRecord, ABC):
    pass


class AttachmentHeader(WireHeader):
    @model_validator(mode='before')
    @classmethod
    def task_notification(cls, value: dict) -> dict:
        if (value.get('type') == QueuedTaskNotification.wire_tag
                and value.get('origin', {}).get('kind') == TaskNotification.wire_tag):
            return value
        return {'type': ForeignAttachment.wire_tag}


class AttachmentPayload(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = AttachmentHeader

    @abstractmethod
    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]: ...


class QueuedTaskNotification(TaskContent, AttachmentPayload):
    wire_tag = 'queued_command'
    text: TaskEnvelope = Field(alias='prompt')
    origin: TaskNotification


class ForeignAttachment(AttachmentPayload):
    wire_tag = 'foreign'

    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]:
        return ()


class Attachment(ClaudeRecord, TimedRecord):
    wire_tag = 'attachment'
    attachment: AttachmentPayload.declared_union()

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return self.attachment.updates(self)


class QueueOperation(MetadataRecord):
    wire_tag = 'queue-operation'


class Mode(MetadataRecord):
    wire_tag = 'mode'


class PermissionMode(MetadataRecord):
    wire_tag = 'permission-mode'


class AtisLatch(MetadataRecord):
    wire_tag = 'atis-latch'


class LastPrompt(MetadataRecord):
    wire_tag = 'last-prompt'


class AiTitle(MetadataRecord):
    wire_tag = 'ai-title'


class PrLink(MetadataRecord):
    wire_tag = 'pr-link'


class FileHistorySnapshot(MetadataRecord):
    wire_tag = 'file-history-snapshot'


class FileHistoryDelta(MetadataRecord):
    wire_tag = 'file-history-delta'


class CostState(MetadataRecord):
    wire_tag = 'cost-state'


class LocalCommandOutput(MetadataRecord):
    """A terminal-only slash command and its output."""
    wire_tag = 'local-command'


class HarnessNote(MetadataRecord):
    """Text the harness adds for the model without a sender: reminders, skill bodies, image notes."""
    wire_tag = 'harness-note'


class ForkContextReference(MetadataRecord):
    """A forked subagent starts from its parent's context, stored in the parent's transcript."""
    wire_tag = 'fork-context-ref'
    parentSessionId: str

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (HistoryNotice(f'Context inherited from session {self.parentSessionId} is not imported.'),)


CLAUDE_RECORD = TypeAdapter(ClaudeRecord.declared_union())
