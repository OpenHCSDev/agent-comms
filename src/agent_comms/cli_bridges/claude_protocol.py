"""Claude's completed JSONL blocks own transcript items and activity."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Annotated, ClassVar, Literal
from xml.etree import ElementTree

from metaclass_registry import RegistryConfig
from pydantic import AliasChoices, AliasPath, BeforeValidator, Field, JsonValue, TypeAdapter, computed_field, field_validator, model_validator

from agent_comms.activity import ActivityState
from agent_comms.cli_protocol import TaggedWire, WireHeader, WireModel
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.tool_declaration import ClaudeToolInput, ToolDeclaration
from agent_comms.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    ContextTranscript,
    ThinkingTranscript,
    TranscriptEvent,
    UserTranscript,
)

from .shared import (
    ActiveItem,
    FinishedItem,
    CompletedItem,
    StartedItem,
    AllTurnsEnded,
    ToolCall,
    TranscriptUpdate,
    TurnStarted,
    IncomingFrame,
    MonitorFrame,
    MonitorPart,
    MonitorProbe,
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
    def decode_line(cls, line: str) -> MonitorFrame | None:
        if line.startswith(MonitorProbe.wire_prefix):
            return MONITOR_FRAME.validate_json(line)
        # Every event is painted by its task envelope. Only our declared line
        # protocol can additionally confirm comms delivery.
        if not line.startswith('"' + cls.HEADER + ' · '):
            return None
        raw_header, separator, _ = line.partition(r'\n')
        if not separator or not line.endswith('"'):
            return None
        columns = raw_header[1:].rsplit(' · ', 2)
        if len(columns) != 3:
            return None
        number, slash, total = columns[-1].partition('/')
        if not slash or not number.isdecimal() or not total.isdecimal():
            return None
        decoded = cls.JSON_TEXT.validate_json(line)
        header, _, body = decoded.partition('\n')
        sender, message_id, _ = header.removeprefix(cls.HEADER + ' · ').rsplit(' · ', 2)
        return MonitorPart(id=message_id, sender=sender, number=int(number),
                           total=int(total), body=body)

@dataclass
class ClaudeSession:
    turn: str = ''
    tools: dict[str, ToolCall] = field(default_factory=dict)
    catching_up: bool = True

    def started(self, call: ToolCall) -> TranscriptUpdate:
        self.tools[call.id] = call
        if not self.turn:
            if self.catching_up:
                return ActiveItem(self.turn, call.id, ActivityState.WORKING, call.title)
            raise ValueError('Claude started a tool before identifying its turn')
        return StartedItem(self.turn, call.id, ActivityState.WORKING, call.title, call)

    def ended(self) -> AllTurnsEnded:
        self.tools.clear()
        return AllTurnsEnded()

    def result_for(self, tool_id: str) -> ToolCall | None:
        call = self.tools.pop(tool_id, None)
        if call is None and not self.catching_up:
            raise ValueError(f'Claude result has no tool start: {tool_id}')
        return call

    def finished(self, item: str, events: tuple[TranscriptEvent, ...]) -> tuple[TranscriptUpdate, ...]:
        if not self.turn:
            if self.catching_up:
                return ()  # Its prompt lies before the bounded initial window.
            raise ValueError('Claude completed an item before identifying its turn')
        return (FinishedItem(self.turn, item, events),)


class TimedRecord(WireModel):
    uuid: str
    timestamp: float

    @field_validator('timestamp', mode='before')
    @classmethod
    def epoch(cls, value: str) -> float:
        return datetime.fromisoformat(value).timestamp()


class ClaudeHeader(WireHeader):
    type: str = Field(validation_alias=AliasChoices('subtype', 'type'))

    @model_validator(mode='before')
    @classmethod
    def local_command(cls, value: dict) -> dict:
        # Local slash commands (/login, /cost) are written as user records
        # without a prompt; they never reach the model. Decode that shape here.
        if value.get('type') == 'user':
            if value.get('isCompactSummary') is True:
                return {'type': CompactUserRecord.wire_tag}
            if value.get('origin', {}).get('kind') == TaskEnvelope.tag:
                return {'type': TaskUserRecord.wire_tag}
            if 'promptId' not in value:
                return {'type': LocalCommandOutput.wire_tag}
        if value.get('type') == QueuedTaskNotification.container_tag:
            attachment = value[QueuedTaskNotification.container_tag]
            if (attachment.get('type') == QueuedTaskNotification.wire_tag
                    and attachment.get('origin', {}).get('kind') == TaskEnvelope.tag):
                return {'type': QueuedTaskNotification.wire_tag}
        # A subtype discriminates only a system envelope, never an ordinary message.
        return value if value.get('type') == 'system' else {'type': value.get('type')}


class ClaudeRecord(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = ClaudeHeader

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return ()


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
        return (ActiveItem(session.turn, record.uuid, ActivityState.THINKING, ''),)


class ThinkingBlock(RedactedThinking):
    wire_tag = 'thinking'
    thinking: str

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        activity = super().updates(record, session)
        if not self.thinking:
            return activity
        return (*activity, *session.finished(
            record.uuid, (ThinkingTranscript(self.thinking, timestamp=record.timestamp),)))


class ToolUse(AssistantBlock):
    wire_tag = 'tool_use'
    id: str
    name: str
    input: JsonValue

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        arguments = ToolDeclaration.for_name(self.name).recorded_arguments(ClaudeToolInput, self.input)
        call = ToolCall(RecordedEventTime(record.timestamp), self.id, self.name, arguments)
        return (session.started(call),)


class AssistantRecord(ClaudeRecord, TimedRecord):
    wire_tag = 'assistant'
    content: list[AssistantBlock.declared_union()] = Field(validation_alias=AliasPath('message', 'content'))

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return tuple(update for block in self.content for update in block.updates(self, session))


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
    """Who wrote a user record. Only a person's prompt is shown as input."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = KindHeader

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return ()


class HumanPrompt(PromptOrigin):
    wire_tag = 'human'

    def prompt(self, record: TimedRecord, text: str,
               session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.finished(record.uuid, (UserTranscript(text, timestamp=record.timestamp),))


class PeerMessage(PromptOrigin):
    wire_tag = 'peer'


class AutoContinuation(PromptOrigin):
    wire_tag = 'auto-continuation'


class UserBlock(ResultPart, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (TurnStarted(),)


class UserText(TextContent, UserBlock):
    wire_tag = 'text'

    def updates(self, record: UserRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        if self.text.startswith('[Request interrupted by user'):
            return (session.ended(),)
        return super().updates(record, session) + record.origin.prompt(record, self.text, session)


class UserImage(ImageContent, UserBlock):
    wire_tag = 'image'


class ContentBlocks(WireModel):
    """Claude's string shorthand is decoded once for both content families."""

    @field_validator('content', mode='before', check_fields=False)
    @classmethod
    def blocks(cls, value: str | list) -> list:
        return [{'type': 'text', 'text': value}] if isinstance(value, str) else value


class TaskEnvelope(WireModel):
    """The notification owns its displayed fields and optional comms event."""
    tag: ClassVar[str] = 'task-notification'
    summary: str
    event: str = ''

    @classmethod
    def from_text(cls, text: str) -> TaskEnvelope:
        # Claude may append a sibling system-reminder after the envelope.
        envelope, terminator, _ = text.partition('</task-notification>')
        root = ElementTree.fromstring(envelope + terminator)
        if root.tag != cls.tag:
            raise ValueError('Expected a task-notification envelope')
        return cls.model_validate({name: ''.join(child.itertext()) for name in cls.model_fields
                                   if (child := root.find(name)) is not None})

    def updates(self, record: TimedRecord) -> tuple[TranscriptUpdate, ...]:
        lines, observations = [self.summary], []
        for line in self.event.splitlines():
            if (frame := ClaudeMonitorFrame.decode_line(line)) is None:
                lines.append(line)
            else:
                observations.append(MonitorObservation(frame, ObservedMonitor(record.uuid, record.timestamp)))
        return (*observations, FinishedItem(record.uuid, record.uuid,
                   (NoticeTranscript('\n'.join(lines), timestamp=record.timestamp),)))


class ResultMessage(ContentBlocks):
    content: list[ResultPart.declared_union()]

    def result_text(self) -> str:
        return '\n'.join(part.result_text() for part in self.content)


class ToolResult(ResultMessage, UserBlock):
    wire_tag = 'tool_result'
    tool_use_id: str
    is_error: bool = False

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        call = session.result_for(self.tool_use_id)
        if call is None:
            return ()  # Only an initial window may lack the original tool start.
        text = self.result_text()
        return (CompletedItem(session.turn, self.tool_use_id, call.finished(text, not self.is_error)),
                TurnStarted())


class UserMessage(ContentBlocks):
    content: list[UserBlock.declared_union()]


class PromptRecord(ClaudeRecord, TimedRecord, ABC):
    promptId: str
    isMeta: bool = False

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        session.turn = self.promptId
        return () if self.isMeta else self.present(session)

    @abstractmethod
    def present(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]: ...


class UserRecord(PromptRecord):
    wire_tag = 'user'
    message: UserMessage
    origin: PromptOrigin.declared_union() = Field(default_factory=HumanPrompt)

    def present(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return tuple(update for block in self.message.content for update in block.updates(self, session))


class CompactUserRecord(PromptRecord):
    wire_tag = 'compact-user'
    message: ResultMessage

    def present(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.finished(self.uuid, (ContextTranscript(self.message.result_text(), timestamp=self.timestamp),))


class TaskUserRecord(ClaudeRecord, TimedRecord):
    wire_tag = 'task-user'
    message: UserMessage

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return tuple(update for block in self.message.content
                     for update in TaskEnvelope.from_text(block.result_text()).updates(self))


class TurnDuration(ClaudeRecord):
    wire_tag = 'turn_duration'

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (session.ended(),)


class CompactBoundary(ClaudeRecord, TimedRecord):
    wire_tag = 'compact_boundary'

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.finished(self.uuid, (NoticeTranscript('Context compacted', timestamp=self.timestamp),))


class QueuedTaskNotification(ClaudeRecord, TimedRecord):
    container_tag: ClassVar[str] = 'attachment'
    wire_tag = 'queued_command'
    text: Annotated[TaskEnvelope, BeforeValidator(TaskEnvelope.from_text)] = Field(
        validation_alias=AliasPath(container_tag, 'prompt'))

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return self.text.updates(self)


class MetadataRecord(ClaudeRecord):
    wire_kinds: ClassVar[tuple[str, ...]] = (
        'stop_hook_summary', 'informational', 'scheduled_task_fire', 'local_command',
        'away_summary', 'agents_killed', 'model_refusal_no_fallback', 'queue-operation',
        'mode', 'permission-mode', 'atis-latch', 'last-prompt', 'ai-title', 'pr-link',
        'file-history-snapshot', 'file-history-delta', 'cost-state', QueuedTaskNotification.container_tag,
    )
    wire_tag = wire_kinds[0]
    kind: Literal[*wire_kinds] = Field(
        validation_alias=ClaudeHeader.model_fields['type'].validation_alias, exclude=True)

    @classmethod
    def wire_tags(cls) -> tuple[str, ...]:
        return cls.wire_kinds

    @computed_field
    @property
    def type(self) -> str:
        return self.kind


class LocalCommandOutput(ClaudeRecord):
    """A terminal-only slash command and its output."""
    wire_tag = 'local-command'


CLAUDE_RECORD = TypeAdapter(ClaudeRecord.declared_union())
