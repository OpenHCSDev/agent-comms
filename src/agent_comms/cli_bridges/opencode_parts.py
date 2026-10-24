"""OpenCode message parts own their Core history, drafts and activity."""
from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from agent_comms.cli_protocol import TaggedWire, TextWire, WireHeader
from agent_comms.field_codec import FieldCodec
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.runtime_updates import (
    CompletedItem,
    HiddenItem,
    HistoryNotice,
    RuntimeUpdate,
    StartedItem,
    Summarized,
    TextDraft,
    ToolDraft,
)
from agent_comms.tool_calls import CliToolArguments, ToolCall
from agent_comms.transcript_events import (
    AssistantTranscript,
    ContextTranscript,
    LiveTextTranscript,
    ThinkingTranscript,
    UserTranscript,
)
from agent_comms.turn_item import NativeItemId, NativeTurnId
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase, TurnPhase

from ..wire_models import WireModel


class MessageTime(WireModel):
    created: int | float
    completed: int | float | None = None


class RoleHeader(WireHeader):
    @model_validator(mode='before')
    @classmethod
    def summary(cls, value: dict) -> dict:
        # The assistant message a compaction writes is flagged as the summary.
        if value.get('role') == AssistantMessage.declared_name and value.get('summary') is True:
            return {'type': SummaryMessage.declared_name}
        return {'type': value.get('role')}


class MessageInfo(TaggedWire, ABC, affix=""):
    header = RoleHeader
    id: str
    sessionID: str
    time: MessageTime

    @property
    def turn(self) -> NativeTurnId:
        return FieldCodec.decode(NativeTurnId, self.id)

    @abstractmethod
    def present(self, part: Part) -> tuple[RuntimeUpdate, ...]: ...

    def finished(self, part: Part) -> bool:
        return part.finished(self)


class UserMessage(MessageInfo, declared_name='user'):
    role: Literal['user']

    def finished(self, part: Part) -> bool:
        return True  # A prompt is complete once saved; it has no completion time.

    def present(self, part: Part) -> tuple[RuntimeUpdate, ...]:
        # Wire history owns incoming bus messages; the live view never echoes them as CLI output.
        return part.prompted(self)


class AssistantMessage(MessageInfo, declared_name='assistant'):
    role: Literal['assistant']
    parentID: str

    def present(self, part: Part) -> tuple[RuntimeUpdate, ...]:
        return part.present(self)


class SummaryMessage(AssistantMessage, declared_name='summary'):
    """OpenCode's summary of the conversation before a compaction: shown live as the reply it
    is, and for history it replaces everything before it."""
    role: Literal['assistant']
    summary: Literal[True]

    def present(self, part: Part) -> tuple[RuntimeUpdate, ...]:
        return (*part.present(self), *part.summarized())


class Part(TaggedWire, ABC, affix=""):
    id: str
    sessionID: str
    messageID: str

    def matches(self, session: str, text: str) -> bool:
        return False

    def finished(self, message: MessageInfo) -> bool:
        return message.time.completed is not None

    @abstractmethod
    def present(self, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]: ...

    def prompted(self, message: UserMessage) -> tuple[RuntimeUpdate, ...]:
        """This part in a user's recorded message, kept for history only."""
        return ()

    def summarized(self) -> tuple[RuntimeUpdate, ...]:
        """This part in a compaction summary."""
        return ()


class PartTime(WireModel):
    start: int | float
    end: int | float | None = None


class TextualPart(Part, ABC, declared_name=None):
    text: str
    time: PartTime | None = None
    event_type: ClassVar[builtins.type[LiveTextTranscript]]
    phase: ClassVar[TurnPhase]

    def timestamp(self, message: MessageInfo) -> float:
        return (self.time.start if self.time is not None else message.time.created) / 1000

    def accumulator(self, message: MessageInfo) -> TextDraft:
        return TextDraft.from_parts(
            self.event_type,
            (self.text,),
            RecordedEventTime(self.timestamp(message)),
            NativeItemId(self.id),
        )

    def finished(self, message: MessageInfo) -> bool:
        return super().finished(message) or (self.time is not None and self.time.end is not None)

    def present(self, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]:
        accumulator = self.accumulator(message)
        if self.finished(message):
            return (CompletedItem(message.turn, NativeItemId(self.id), accumulator.events),)
        return (StartedItem(message.turn, NativeItemId(self.id), self.phase, accumulator),)


class TextPart(TextWire, TextualPart, declared_name='text'):
    event_type = AssistantTranscript
    phase = ModelWaitPhase('Replying')

    def matches(self, session: str, text: str) -> bool:
        return self.sessionID == session and self.text == text

    def prompted(self, message: UserMessage) -> tuple[RuntimeUpdate, ...]:
        return (
            HiddenItem(
                (
                    UserTranscript(
                        self.text, position=NativeItemId(self.id), timestamp=self.timestamp(message)
                    ),
                )
            ),
        )

    def summarized(self) -> tuple[RuntimeUpdate, ...]:
        return (Summarized(self.text),)


class ReasoningPart(TextualPart, declared_name='reasoning'):
    event_type = ThinkingTranscript
    phase = ModelWaitPhase()


class ToolArguments(WireModel):
    """Retain arbitrary tool inputs, declaring OpenCode's native path spelling."""
    model_config = ConfigDict(strict=True, extra='allow')
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    path: str | None = Field(default=None, alias='filePath')


class StatusHeader(WireHeader):
    type: str = Field(alias='status')


class ToolState(TaggedWire, ABC, affix=""):
    header = StatusHeader
    input: ToolArguments
    finished: ClassVar[bool] = False

    @abstractmethod
    def present(self, part: ToolPart, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]: ...

    def call(self, part: ToolPart) -> ToolCall:
        sent = self.input.model_dump(by_alias=True, exclude_unset=True)
        shown = ToolCall(part.callID, part.tool, self.input.model_dump(exclude_unset=True))
        return CliToolArguments.decode(shown, sent)


class PendingTool(ToolState, declared_name='pending'):

    def started_at(self, message: AssistantMessage) -> RecordedEventTime:
        return RecordedEventTime(message.time.created / 1000)

    def present(self, part: ToolPart, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]:
        call = self.call(part)
        started_at = self.started_at(message)
        return (
            StartedItem(
                message.turn,
                NativeItemId(part.id),
                ToolRunningPhase(tools=((part.callID, call.title),)),
                ToolDraft(started_at, (call.started(NativeItemId(part.id), started_at.at),)),
            ),
        )


class RunningTool(PendingTool, declared_name='running'):
    time: PartTime

    def started_at(self, message: AssistantMessage) -> RecordedEventTime:
        return RecordedEventTime(self.time.start / 1000)


class EndedPartTime(PartTime):
    end: int | float = Field(...)


class EndedTool(ToolState, ABC):
    time: EndedPartTime
    ok: ClassVar[bool]
    finished = True

    @property
    @abstractmethod
    def result(self) -> str: ...

    def present(self, part: ToolPart, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]:
        call = self.call(part)
        return (CompletedItem(message.turn, NativeItemId(part.id), (
            call.started(NativeItemId(part.id), self.time.start / 1000),
            call.ended(self.result, self.ok, NativeItemId(part.id), self.time.end / 1000),
        )),)


class CompletedTool(EndedTool, declared_name='completed'):
    output: str
    ok = True

    @property
    def result(self) -> str:
        return self.output


class FailedTool(EndedTool, declared_name='error'):
    error: str
    ok = False

    @property
    def result(self) -> str:
        return self.error


class ToolPart(Part, declared_name='tool'):
    callID: str
    tool: str
    state: ToolState.declared_union()

    def finished(self, message: MessageInfo) -> bool:
        return self.state.finished

    def present(self, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]:
        return self.state.present(self, message)


class StructuralPart(Part, ABC, declared_name=None):
    """Native bookkeeping and input attachments are not assistant transcript items."""
    def present(self, message: AssistantMessage) -> tuple[RuntimeUpdate, ...]:
        return ()


class FilePart(StructuralPart, declared_name='file'):
    filename: str = ''
    mime: str = ''

    def prompted(self, message: UserMessage) -> tuple[RuntimeUpdate, ...]:
        return (
            HistoryNotice("Attachment contents are not imported; source references are retained."),
            HiddenItem(
                (
                    ContextTranscript(
                        f"[Historical attachment: {self.filename or self.mime}]",
                        position=NativeItemId(self.id),
                        timestamp=message.time.created / 1000,
                    ),
                )
            ),
        )


class AgentPart(StructuralPart, declared_name='agent'):
    pass


class SubtaskPart(StructuralPart, declared_name='subtask'):
    pass


class CompactionPart(StructuralPart, declared_name='compaction'):
    pass


class RetryPart(StructuralPart, declared_name='retry'):
    pass


class StepStartPart(StructuralPart, declared_name='step-start'):
    pass


class StepFinishPart(StructuralPart, declared_name='step-finish'):
    pass


class SnapshotPart(StructuralPart, declared_name='snapshot'):
    pass


class PatchPart(StructuralPart, declared_name='patch'):
    pass


class SavedMessage(WireModel):
    info: MessageInfo.declared_union()
    parts: list[Part.declared_union()]

    def publications(self) -> tuple[RuntimeUpdate, ...]:
        """Its finished parts. An unfinished part is published from its own deltas, from its
        start, never from a snapshot that overlaps them; to history it adds nothing."""
        return tuple(update for part in self.parts if self.info.finished(part) for update in self.info.present(part))
