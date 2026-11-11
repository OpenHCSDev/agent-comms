"""The original Codex stored record format and its authenticated historical source."""

from __future__ import annotations

import hashlib
import json
from abc import ABC
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import Annotated

from pydantic import (
    BeforeValidator,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from agent_comms.cli_bridges.cli_protocol import Declared, HistoryRecord
from agent_comms.cli_bridges.cli_protocol import OpenWire
from agent_comms.cli_bridges.cli_protocol import TimestampedWire
from agent_comms.cli_bridges.cli_protocol import WireHeader
from agent_comms.execution.turn_context import HistoricalInstructionProvenance
from agent_comms.nominal.field_codec import ToolCallId
from agent_comms.cli_bridges.runtime_updates import HiddenItem
from agent_comms.cli_bridges.runtime_updates import HistoryNotice
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate
from agent_comms.cli_bridges.runtime_updates import SessionFacts
from agent_comms.cli_bridges.runtime_updates import Summarized
from agent_comms.transport.tool_declaration import ToolDeclaration
from agent_comms.transcript.transcript_events import AssistantTranscript
from agent_comms.transcript.transcript_events import ToolEndTranscript
from agent_comms.transcript.transcript_events import ToolStartTranscript
from agent_comms.transcript.transcript_events import UserTranscript
from agent_comms.cli_bridges.wire_models import WireModel


@dataclass(frozen=True)
class CodexRolloutProvenance(HistoricalInstructionProvenance):
    """Authenticate historical role-record bytes; never current instructions."""

    path: str
    offset: int
    length: int
    sha256: str
    instruction: int

    def instruction_position(self) -> tuple[int, int]:
        return self.offset, self.instruction

    def public_description(self) -> str:
        return (f"Historical Codex instruction · {self.path} · record bytes "
                f"{self.offset}:{self.offset + self.length} · item {self.instruction} "
                "· not current instructions")

    def public_text(self, comms) -> str:
        if min(self.offset, self.length, self.instruction) < 0:
            raise ValueError("Historical Codex source coordinates must be nonnegative")
        with Path(self.path).open("rb") as source:
            source.seek(self.offset)
            raw = source.read(self.length)
        if len(raw) != self.length or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("Historical Codex source record changed or is unavailable")
        try:
            instruction = ROLLOUT_RECORD.validate_json(raw).instructions()[self.instruction]
        except IndexError as error:
            raise ValueError("Historical instruction is outside its original record") from error
        return instruction.text


@dataclass
class RolloutState:
    """Tool names by call id: a result names only its call."""
    tools: dict[ToolCallId, str] = field(default_factory=dict)


class ContentPart(WireModel):
    """Text parts carry `text`; images, audio and refusals carry none and add nothing."""
    text: str = ''


class RolloutItemHeader(WireHeader):
    @model_validator(mode='before')
    @classmethod
    def role(cls, value: dict) -> dict:
        # A message's role selects its member: the role is the item kind that matters.
        return {'type': '/'.join(part for part in (value.get('type'), value.get('role')) if part)}


class RolloutItem(OpenWire, ABC, affix=""):
    header = RolloutItemHeader

    def updates(self, state: RolloutState, position: int, timestamp: float) -> tuple[RuntimeUpdate, ...]:
        return ()

    def instructions(self) -> tuple[InstructionMessage, ...]:
        return ()

    def request(self) -> str:
        return ''


class ForeignItem(RolloutItem, declared_name='foreign'):
    """Reasoning, searches and other items that are not portable conversation."""


class MessageItem(RolloutItem, ABC, declared_name=None):
    content: list[ContentPart]

    @field_validator('content', mode='before')
    @classmethod
    def parts(cls, value: str | list) -> list:
        return [{'text': value}] if isinstance(value, str) else value

    @property
    def text(self) -> str:
        return '\n'.join(part.text for part in self.content if part.text)


class UserMessageItem(MessageItem, declared_name='message/user'):

    def updates(self, state: RolloutState, position: int, timestamp: float) -> tuple[RuntimeUpdate, ...]:
        return (HiddenItem((UserTranscript(self.text, position=position, timestamp=timestamp),)),)

    def request(self) -> str:
        return self.text


class AssistantMessageItem(MessageItem, declared_name='message/assistant'):

    def updates(self, state: RolloutState, position: int, timestamp: float) -> tuple[RuntimeUpdate, ...]:
        return (HiddenItem((AssistantTranscript(self.text, position=position, timestamp=timestamp),)),)


class InstructionMessage(MessageItem, ABC, declared_name=None):
    """Instructions the source session ran under: kept as references to their bytes, never as
    messages, because they were that session's instructions and are not this thread's."""

    def instructions(self) -> tuple[InstructionMessage, ...]:
        return (self,)


class SystemMessageItem(InstructionMessage, declared_name='message/system'):
    pass


class DeveloperMessageItem(InstructionMessage, declared_name='message/developer'):
    pass


class FunctionCallItem(RolloutItem, declared_name='function_call'):
    name: str
    call_id: ToolCallId
    arguments: str = ''

    @property
    def raw_input(self) -> dict[str, JsonValue]:
        return TypeAdapter(dict[str, JsonValue]).validate_json(self.arguments)

    def updates(self, state: RolloutState, position: int, timestamp: float) -> tuple[RuntimeUpdate, ...]:
        state.tools[self.call_id] = self.name
        return (HiddenItem((ToolStartTranscript(
            position=position, timestamp=timestamp, tool_call_id=self.call_id, tool_name=self.name, raw_input=self.raw_input,
            tool=ToolDeclaration.record(self.name, self.raw_input, call_id=self.call_id)),)),)


class CustomToolCallItem(FunctionCallItem, declared_name='custom_tool_call'):
    """A freeform tool (apply_patch): its input is text, not JSON arguments."""
    arguments: str = Field(default='', alias='input')

    @property
    def raw_input(self) -> dict[str, JsonValue]:
        return {'input': self.arguments}


def output_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class FunctionCallOutputItem(RolloutItem, declared_name='function_call_output'):
    call_id: ToolCallId
    output: Annotated[str, BeforeValidator(output_text)] = ''

    def updates(self, state: RolloutState, position: int, timestamp: float) -> tuple[RuntimeUpdate, ...]:
        return (HiddenItem((ToolEndTranscript(
            position=position, timestamp=timestamp, tool_call_id=self.call_id,
            tool_name=state.tools.get(self.call_id, ''), text=self.output),)),)


class CustomToolCallOutputItem(FunctionCallOutputItem, declared_name='custom_tool_call_output'):
    pass


class RolloutRecord(TimestampedWire, OpenWire, HistoryRecord, affix=""):
    """One rollout line. Conversation is in the items a record carries; the record adds what
    it says about the session."""

    def entries(self) -> tuple[RolloutItem, ...]:
        return ()

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        return ()

    def updates(self, state: RolloutState, position: int) -> tuple[RuntimeUpdate, ...]:
        return (*self.own_updates(), *(update for item in self.entries() for update in item.updates(state, position, self.timestamp)))

    def instructions(self) -> tuple[InstructionMessage, ...]:
        return tuple(instruction for item in self.entries() for instruction in item.instructions())

    def request(self) -> str:
        return next((text for item in reversed(self.entries()) if (text := item.request())), '')

    @property
    def session(self) -> SessionMeta:
        raise ValueError('A Codex rollout starts with its session metadata.')


class ForeignRecord(RolloutRecord, declared_name='foreign'):
    """Events the rollout mirrors from items (event_msg) and other bookkeeping."""


class SessionMeta(WireModel):
    id: str
    cwd: str = ''


class SessionMetaRecord(RolloutRecord, declared_name='session_meta'):
    payload: SessionMeta

    @property
    def session(self) -> SessionMeta:
        return self.payload

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        return (SessionFacts(source_id=self.payload.id, project=self.payload.cwd),)


class TurnContext(WireModel):
    cwd: str = ''


class TurnContextRecord(RolloutRecord, declared_name='turn_context'):
    payload: TurnContext

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        return (SessionFacts(project=self.payload.cwd),)


class Compaction(WireModel):
    message: str = ''
    summary: str = ''
    replacement_history: list[Declared[RolloutItem]] = Field(default_factory=list)
    guardian_history: list[JsonValue] = Field(default_factory=list)


class CompactedRecord(RolloutRecord, declared_name='compacted'):
    """Codex replaced the conversation with a summary and the items it kept."""
    payload: Compaction

    def begins_history(self) -> bool:
        return True

    def entries(self) -> tuple[RolloutItem, ...]:
        return tuple(self.payload.replacement_history)

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        private = (HistoryNotice('Codex guardian history is source-private and was not imported.'),
                   ) if self.payload.guardian_history else ()
        return (Summarized(self.payload.message or self.payload.summary), *private)


class ResponseItemRecord(RolloutRecord, declared_name='response_item'):
    payload: Declared[RolloutItem]

    def entries(self) -> tuple[RolloutItem, ...]:
        return (self.payload,)


ROLLOUT_RECORD = WireModel.adapter(RolloutRecord.declared_union())
