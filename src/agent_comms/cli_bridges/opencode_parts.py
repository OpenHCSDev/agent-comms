"""OpenCode message parts own their Core history, drafts and activity."""
from __future__ import annotations

from abc import ABC, abstractmethod
import builtins
from dataclasses import replace
from typing import ClassVar, Literal

from metaclass_registry import RegistryConfig
from pydantic import ConfigDict, Field, JsonValue

from agent_comms.activity import ActivityState
from agent_comms.transcript_events import (
    AssistantTranscript, ThinkingTranscript, TranscriptEvent, ToolStartTranscript,
)
from .shared import (
    ActiveItem, DraftItem, FinishedItem, TaggedWire, TranscriptUpdate,
    WireHeader, WireModel,
)


class MessageTime(WireModel):
    created: int | float
    completed: int | float | None = None


class RoleHeader(WireHeader):
    type: str = Field(alias='role')


class MessageInfo(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = RoleHeader
    id: str
    sessionID: str
    time: MessageTime

    @abstractmethod
    def present(self, part: Part) -> tuple[TranscriptUpdate, ...]: ...

    @abstractmethod
    def append_text(self, part: Part, delta: str) -> tuple[TranscriptUpdate, ...]: ...


class UserMessage(MessageInfo):
    wire_tag = 'user'
    role: Literal['user']

    def present(self, part: Part) -> tuple[TranscriptUpdate, ...]:
        # Wire history owns incoming bus messages; never echo them as CLI output.
        return ()

    def append_text(self, part: Part, delta: str) -> tuple[TranscriptUpdate, ...]:
        return ()


class AssistantMessage(MessageInfo):
    wire_tag = 'assistant'
    role: Literal['assistant']
    parentID: str

    def present(self, part: Part) -> tuple[TranscriptUpdate, ...]:
        return part.present(self)

    def append_text(self, part: Part, delta: str) -> tuple[TranscriptUpdate, ...]:
        return part.append_text(delta, self)


class Part(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    id: str
    sessionID: str
    messageID: str

    def matches(self, session: str, text: str) -> bool:
        return False

    def append_text(self, delta: str, message: MessageInfo) -> tuple[TranscriptUpdate, ...]:
        raise ValueError(f'OpenCode sent a text delta for {self.wire_tag}')

    def finished(self, message: MessageInfo) -> bool:
        return message.time.completed is not None

    @abstractmethod
    def present(self, message: AssistantMessage) -> tuple[TranscriptUpdate, ...]: ...


class PartTime(WireModel):
    start: int | float
    end: int | float | None = None


class TextualPart(Part, ABC):
    text: str
    time: PartTime | None = None
    event_type: ClassVar[builtins.type[TranscriptEvent]]
    activity: ClassVar[ActivityState]
    detail: ClassVar[str]

    def append_text(self, delta: str, message: MessageInfo) -> tuple[TranscriptUpdate, ...]:
        self.text += delta
        return (DraftItem(message.id, self.id, self.events(message)),)

    def events(self, message: MessageInfo) -> tuple[TranscriptEvent, ...]:
        timestamp = (self.time.start if self.time is not None else message.time.created) / 1000
        return (self.event_type(self.text, timestamp=timestamp),) if self.text else ()

    def finished(self, message: MessageInfo) -> bool:
        return super().finished(message) or (self.time is not None and self.time.end is not None)

    def present(self, message: AssistantMessage) -> tuple[TranscriptUpdate, ...]:
        events = self.events(message)
        if self.finished(message):
            return (FinishedItem(message.id, self.id, events),)
        return (ActiveItem(message.id, self.id, self.activity, self.detail),
                DraftItem(message.id, self.id, events))


class TextPart(TextualPart):
    wire_tag = 'text'
    event_type = AssistantTranscript
    activity = ActivityState.WORKING
    detail = 'Replying'

    def matches(self, session: str, text: str) -> bool:
        return self.sessionID == session and self.text == text


class ReasoningPart(TextualPart):
    wire_tag = 'reasoning'
    event_type = ThinkingTranscript
    activity = ActivityState.THINKING
    detail = ''


class ToolArguments(WireModel):
    """Retain arbitrary tool inputs, declaring OpenCode's native path spelling."""
    model_config = ConfigDict(strict=True, extra='allow')
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    filePath: str | None = Field(default=None, serialization_alias='file_path')


class StatusHeader(WireHeader):
    type: str = Field(alias='status')


class ToolState(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = StatusHeader
    input: ToolArguments
    finished: ClassVar[bool] = False

    @abstractmethod
    def present(self, part: ToolPart, message: AssistantMessage) -> tuple[TranscriptUpdate, ...]: ...

    def call(self, part: ToolPart) -> ToolStartTranscript:
        return ToolStartTranscript(
            tool_call_id=part.callID, tool_name=part.tool,
            raw_input=self.input.model_dump(by_alias=True, exclude_unset=True),
        )

    def started_at(self, message: MessageInfo) -> float:
        return message.time.created / 1000


class PendingTool(ToolState):
    wire_tag = 'pending'

    def present(self, part: ToolPart, message: AssistantMessage) -> tuple[TranscriptUpdate, ...]:
        return ActiveItem.tool_started(
            message.id, part.id, replace(self.call(part), timestamp=self.started_at(message))
        )


class TimedTool(ToolState, ABC):
    time: PartTime

    def started_at(self, message: MessageInfo) -> float:
        return self.time.start / 1000


class RunningTool(PendingTool, TimedTool):
    wire_tag = 'running'


class EndedTool(TimedTool, ABC):
    ok: ClassVar[bool]
    finished = True

    @property
    @abstractmethod
    def result(self) -> str: ...

    def present(self, part: ToolPart, message: AssistantMessage) -> tuple[TranscriptUpdate, ...]:
        events = replace(self.call(part), timestamp=self.started_at(message)).finished(
            self.result, self.ok
        )
        return (FinishedItem(message.id, part.id, events),)


class CompletedTool(EndedTool):
    wire_tag = 'completed'
    output: str
    ok = True

    @property
    def result(self) -> str:
        return self.output


class FailedTool(EndedTool):
    wire_tag = 'error'
    error: str
    ok = False

    @property
    def result(self) -> str:
        return self.error


class ToolPart(Part):
    wire_tag = 'tool'
    callID: str
    tool: str
    state: ToolState.declared_union()

    def finished(self, message: MessageInfo) -> bool:
        return self.state.finished

    def present(self, message: AssistantMessage) -> tuple[TranscriptUpdate, ...]:
        return self.state.present(self, message)


class StructuralPart(Part, ABC):
    """Native bookkeeping and input attachments are not assistant transcript items."""
    def present(self, message: AssistantMessage) -> tuple[TranscriptUpdate, ...]:
        return ()


class FilePart(StructuralPart):
    wire_tag = 'file'


class AgentPart(StructuralPart):
    wire_tag = 'agent'


class SubtaskPart(StructuralPart):
    wire_tag = 'subtask'


class CompactionPart(StructuralPart):
    wire_tag = 'compaction'


class RetryPart(StructuralPart):
    wire_tag = 'retry'


class StepStartPart(StructuralPart):
    wire_tag = 'step-start'


class StepFinishPart(StructuralPart):
    wire_tag = 'step-finish'


class SnapshotPart(StructuralPart):
    wire_tag = 'snapshot'


class PatchPart(StructuralPart):
    wire_tag = 'patch'


class SavedMessage(WireModel):
    info: MessageInfo.declared_union()
    parts: list[Part.declared_union()]

    def publications(self) -> tuple[TranscriptUpdate, ...]:
        return tuple(update for part in self.parts for update in self.info.present(part))
