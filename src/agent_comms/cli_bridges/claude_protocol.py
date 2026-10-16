"""Claude's completed JSONL blocks own transcript items and activity."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from metaclass_registry import RegistryConfig
from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator

from agent_comms.activity import ActivityState
from agent_comms.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    ThinkingTranscript,
    TranscriptEvent,
    ToolStartTranscript,
    UserTranscript,
)
from .shared import (
    ActiveItem, FinishedItem, TaggedWire, TranscriptUpdate, TurnEnded,
    TurnStarted, WireHeader, WireModel,
)


@dataclass
class ClaudeSession:
    turn: str = ''
    tools: dict[str, ToolStartTranscript] = field(default_factory=dict)
    catching_up: bool = True

    def started(self, call: ToolStartTranscript) -> None:
        self.tools[call.tool_call_id] = call

    def result_for(self, tool_id: str) -> ToolStartTranscript | None:
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
    @model_validator(mode='before')
    @classmethod
    def local_command(cls, value: dict) -> dict:
        # Local slash commands (/login, /cost) are written as user records
        # without a prompt; they never reach the model. Decode that shape here.
        if value.get('type') == 'user' and 'promptId' not in value:
            return {'type': LocalCommandOutput.wire_tag}
        return value


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
        call = ToolStartTranscript(tool_call_id=self.id, tool_name=self.name,
                                   raw_input=self.input, timestamp=record.timestamp)
        session.started(call)
        return ActiveItem.tool_started(session.turn, self.id, call)


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


class TaskNotification(PromptOrigin):
    """Monitor output; bus messages in it already reach Toad through Core."""
    wire_tag = 'task-notification'


class PeerMessage(PromptOrigin):
    wire_tag = 'peer'


class AutoContinuation(PromptOrigin):
    wire_tag = 'auto-continuation'


class ToolSupplied(PromptOrigin):
    """Records Claude writes without an origin: tool results and summaries."""


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
        return super().updates(record, session) + record.origin.prompt(record, self.text, session)


class UserImage(UserBlock, ImageContent):
    wire_tag = 'image'


class ContentBlocks(WireModel):
    """Claude's string shorthand is decoded once for both content families."""

    @field_validator('content', mode='before', check_fields=False)
    @classmethod
    def blocks(cls, value: str | list) -> list:
        return [{'type': 'text', 'text': value}] if isinstance(value, str) else value


class ToolResult(UserBlock, ContentBlocks):
    wire_tag = 'tool_result'
    tool_use_id: str
    content: list[ResultPart.declared_union()]
    is_error: bool = False

    def updates(self, record: TimedRecord, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        call = session.result_for(self.tool_use_id)
        if call is None:
            return ()  # Only an initial window may lack the original tool start.
        text = '\n'.join(part.result_text() for part in self.content)
        return (FinishedItem(session.turn, self.tool_use_id, call.finished(text, not self.is_error)),
                ActiveItem(session.turn, self.tool_use_id, ActivityState.WORKING, ''))


class UserMessage(ContentBlocks):
    content: list[UserBlock.declared_union()]


class UserRecord(ClaudeRecord, TimedRecord):
    wire_tag = 'user'
    promptId: str
    message: UserMessage
    origin: PromptOrigin.declared_union() = Field(default_factory=ToolSupplied)
    isMeta: bool = False

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        session.turn = self.promptId
        if self.isMeta:
            return ()
        return tuple(update for block in self.message.content for update in block.updates(self, session))


class SubtypeHeader(WireHeader):
    type: str = Field(alias='subtype')


class SystemEvent(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = SubtypeHeader

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return ()


class TurnDuration(SystemEvent):
    wire_tag = 'turn_duration'

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return (TurnEnded(),)


class CompactBoundary(SystemEvent, TimedRecord):
    wire_tag = 'compact_boundary'

    def updates(self, session: ClaudeSession) -> tuple[TranscriptUpdate, ...]:
        return session.finished(self.uuid, (NoticeTranscript('Context compacted', timestamp=self.timestamp),))


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


class MetadataRecord(ClaudeRecord, ABC):
    pass


class Attachment(MetadataRecord):
    wire_tag = 'attachment'


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


CLAUDE_RECORD = TypeAdapter(ClaudeRecord.declared_union())
