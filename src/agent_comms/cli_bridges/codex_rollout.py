"""Codex's stored rollout: one JSON record per line, in the Responses item format.

The app-server (codex_protocol) reports the same conversation in a different item format; both
decoders produce the same transcript updates.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, ClassVar

from metaclass_registry import RegistryConfig
from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator

from agent_comms.cli_protocol import OpenWire, WireHeader, WireModel
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.transcript_events import AssistantTranscript, ToolEndTranscript, ToolStartTranscript, UserTranscript
from agent_comms.turn_context import CodexRolloutProvenance

from .history import CodexHistory, JsonLinesFile, OfflineHistory
from agent_comms.runtime_updates import (
    FinishedItem, HistoryNotice, RuntimeUpdate, SessionFacts, Summarized,
)


@dataclass
class RolloutState:
    """Tool names by call id: a result names only its call."""
    tools: dict[str, str] = field(default_factory=dict)


class ContentPart(WireModel):
    """Text parts carry `text`; images, audio and refusals carry none and add nothing."""
    text: str = ''


class RolloutItemHeader(WireHeader):
    @model_validator(mode='before')
    @classmethod
    def role(cls, value: dict) -> dict:
        # A message's role selects its member: the role is the item kind that matters.
        return {'type': '/'.join(part for part in (value.get('type'), value.get('role')) if part)}


class RolloutItem(OpenWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    header = RolloutItemHeader

    def updates(self, state: RolloutState, position: int) -> tuple[RuntimeUpdate, ...]:
        return ()

    def instructions(self) -> tuple[InstructionMessage, ...]:
        return ()

    def request(self) -> str:
        return ''


class ForeignItem(RolloutItem):
    """Reasoning, searches and other items that are not portable conversation."""
    wire_tag = 'foreign'


class MessageItem(RolloutItem, ABC):
    id: str = ''
    content: list[ContentPart]

    @field_validator('content', mode='before')
    @classmethod
    def parts(cls, value: str | list) -> list:
        return [{'text': value}] if isinstance(value, str) else value

    @property
    def text(self) -> str:
        return '\n'.join(part.text for part in self.content if part.text)


class UserMessageItem(MessageItem):
    wire_tag = 'message/user'

    def updates(self, state: RolloutState, position: int) -> tuple[RuntimeUpdate, ...]:
        return (FinishedItem('', self.id, (UserTranscript(self.text, position=position),)),)

    def request(self) -> str:
        return self.text


class AssistantMessageItem(MessageItem):
    wire_tag = 'message/assistant'

    def updates(self, state: RolloutState, position: int) -> tuple[RuntimeUpdate, ...]:
        return (FinishedItem('', self.id, (AssistantTranscript(self.text, position=position),)),)


class InstructionMessage(MessageItem, ABC):
    """Instructions the source session ran under: kept as references to their bytes, never as
    messages, because they were that session's instructions and are not this thread's."""

    def instructions(self) -> tuple[InstructionMessage, ...]:
        return (self,)


class SystemMessageItem(InstructionMessage):
    wire_tag = 'message/system'


class DeveloperMessageItem(InstructionMessage):
    wire_tag = 'message/developer'


class FunctionCallItem(RolloutItem):
    wire_tag = 'function_call'
    name: str
    call_id: str = ''
    arguments: str = ''

    @property
    def raw_input(self) -> dict[str, JsonValue]:
        return TypeAdapter(dict[str, JsonValue]).validate_json(self.arguments)

    def updates(self, state: RolloutState, position: int) -> tuple[RuntimeUpdate, ...]:
        state.tools[self.call_id] = self.name
        return (FinishedItem('', self.call_id, (ToolStartTranscript(
            position=position, tool_call_id=self.call_id, tool_name=self.name, raw_input=self.raw_input,
            tool=ToolDeclaration.record(self.name, self.raw_input, call_id=self.call_id)),)),)


class CustomToolCallItem(FunctionCallItem):
    """A freeform tool (apply_patch): its input is text, not JSON arguments."""
    wire_tag = 'custom_tool_call'
    arguments: str = Field(default='', alias='input')

    @property
    def raw_input(self) -> dict[str, JsonValue]:
        return {'input': self.arguments}


class FunctionCallOutputItem(RolloutItem):
    wire_tag = 'function_call_output'
    call_id: str = ''
    output: JsonValue = ''

    def updates(self, state: RolloutState, position: int) -> tuple[RuntimeUpdate, ...]:
        text = self.output if isinstance(self.output, str) else json.dumps(self.output, ensure_ascii=False)
        return (FinishedItem('', self.call_id + ':result', (ToolEndTranscript(
            position=position, tool_call_id=self.call_id, tool_name=state.tools.get(self.call_id, ''), text=text),)),)


class CustomToolCallOutputItem(FunctionCallOutputItem):
    wire_tag = 'custom_tool_call_output'


class RolloutRecord(OpenWire, ABC):
    """One rollout line. Conversation is in the items a record carries; the record adds what
    it says about the session."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    def entries(self) -> tuple[RolloutItem, ...]:
        return ()

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        return ()

    def updates(self, state: RolloutState, position: int) -> tuple[RuntimeUpdate, ...]:
        return (*self.own_updates(), *(update for item in self.entries() for update in item.updates(state, position)))

    def instructions(self) -> tuple[InstructionMessage, ...]:
        return tuple(instruction for item in self.entries() for instruction in item.instructions())

    def request(self) -> str:
        return next((text for item in reversed(self.entries()) if (text := item.request())), '')

    def begins_history(self) -> bool:
        return False

    @property
    def session(self) -> SessionMeta:
        raise ValueError('A Codex rollout starts with its session metadata.')


class ForeignRecord(RolloutRecord):
    """Events the rollout mirrors from items (event_msg) and other bookkeeping."""
    wire_tag = 'foreign'


class SessionMeta(WireModel):
    id: str
    cwd: str = ''


class SessionMetaRecord(RolloutRecord):
    wire_tag = 'session_meta'
    payload: SessionMeta

    @property
    def session(self) -> SessionMeta:
        return self.payload

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        return (SessionFacts(source_id=self.payload.id, project=self.payload.cwd),)


class TurnContext(WireModel):
    cwd: str = ''


class TurnContextRecord(RolloutRecord):
    wire_tag = 'turn_context'
    payload: TurnContext

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        return (SessionFacts(project=self.payload.cwd),)


class Compaction(WireModel):
    message: str = ''
    summary: str = ''
    replacement_history: list[RolloutItem.declared_union()] = Field(default_factory=list)
    guardian_history: list[JsonValue] = Field(default_factory=list)


class CompactedRecord(RolloutRecord):
    """Codex replaced the conversation with a summary and the items it kept."""
    wire_tag = 'compacted'
    payload: Compaction

    def begins_history(self) -> bool:
        return True

    def entries(self) -> tuple[RolloutItem, ...]:
        return tuple(self.payload.replacement_history)

    def own_updates(self) -> tuple[RuntimeUpdate, ...]:
        private = (HistoryNotice('Codex guardian history is source-private and was not imported.'),
                   ) if self.payload.guardian_history else ()
        return (Summarized(self.payload.message or self.payload.summary), *private)


class ResponseItemRecord(RolloutRecord):
    wire_tag = 'response_item'
    payload: RolloutItem.declared_union()

    def entries(self) -> tuple[RolloutItem, ...]:
        return (self.payload,)


ROLLOUT_RECORD = TypeAdapter(RolloutRecord.declared_union())


@dataclass(frozen=True)
class HistoricalInstruction(RuntimeUpdate):
    """Where one instruction of the source session is, so its exact text can be read later."""
    source: CodexRolloutProvenance

    def publish(self, output) -> None:
        pass

    def add_to(self, history) -> None:
        history.instructions.add(self.source)


class CodexRollout(JsonLinesFile, CodexHistory, OfflineHistory):
    """`~/.codex/sessions/.../rollout-*.jsonl`: Codex's stored session, which it appends to while it runs."""

    suffixes = frozenset({'.jsonl'})

    records = ROLLOUT_RECORD
    state = RolloutState

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        """The first record names the session; the selected history may start long after it."""
        for offset, raw in self.complete_lines(stream, 0, boundary):
            record = self.decode_line(raw, offset)
            if self.session_id not in (None, record.session.id):
                raise ValueError('The rollout belongs to a different Codex session.')
            yield from record.updates(self.start_state(), offset)
            return
        raise ValueError('A Codex rollout starts with its session metadata.')

    def record_updates(self, record: RolloutRecord, state: RolloutState, offset: int,
                       raw: bytes) -> Iterator[RuntimeUpdate]:
        yield from record.updates(state, offset)
        digest = hashlib.sha256(raw).hexdigest()
        for index, _ in enumerate(record.instructions()):
            yield HistoricalInstruction(CodexRolloutProvenance(
                str(self.source.resolve()), offset, len(raw), digest, index))
