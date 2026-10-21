"""Inherited capabilities exercised at the real codec and watchdog boundaries."""

from dataclasses import dataclass, field
from typing import Annotated

import pytest
from pydantic import TypeAdapter

from agent_comms.cli_bridges.claude_protocol import AssistantBlock, ResultPart, UserBlock
from agent_comms.cli_bridges.codex_protocol import UserInput
from agent_comms.cli_bridges.opencode_parts import Part
from agent_comms.cli_protocol import TextWire
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import (
    AcceptsNull,
    ExternalJsonShape,
    FieldCodec,
    FieldRepresentation,
    MalformedRecord,
)
from agent_comms.native_startup import NativeStartupPolicy
from agent_comms.pi_events import AgentStart, PiEvent, QueueUpdate
from agent_comms.pi_payloads import (
    Opaque,
    PiContent,
    PiResponseData,
    SessionSwitchData,
    StateData,
    StrictFields,
    TextContent,
    UnknownContent,
)
from agent_comms.turn_output import TurnOutput
from agent_comms.turn_phase import AcceptsPrompt, IdlePhase, ModelWaitPhase, TurnPhase
from agent_comms.turn_watchdog import ModelWait, ProgressWatchdog


class CapabilityRepresentation(DeclaredFamily, FieldRepresentation, affix="Representation"):
    @classmethod
    def encode(cls, value):
        return value

    @classmethod
    def decode(cls, value):
        return FieldCodec.decode(str, value)


class OrdinaryRepresentation(CapabilityRepresentation):
    pass


class NullableRepresentation(AcceptsNull, CapabilityRepresentation):
    @classmethod
    def decode(cls, value):
        return "null decoded" if value is None else super().decode(value)


class ExternalRepresentation(ExternalJsonShape, CapabilityRepresentation):
    @classmethod
    def from_wire(cls, value):
        return FieldCodec.decode(str, value["text"])


def test_accepts_null_owns_null_while_ordinary_representation_uses_structure():
    nullable, = CapabilityRepresentation.members_with(AcceptsNull)
    assert FieldCodec.decode(Annotated[str | None, nullable], None) == "null decoded"
    assert OrdinaryRepresentation not in CapabilityRepresentation.members_with(AcceptsNull)
    assert FieldCodec.decode(Annotated[str | None, OrdinaryRepresentation], None) is None
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(Annotated[str, OrdinaryRepresentation], None)


def test_external_json_shape_owns_from_wire_while_ordinary_representation_decodes():
    external, = CapabilityRepresentation.members_with(ExternalJsonShape)
    assert FieldCodec.decode(Annotated[str, external], {"text": "visible"}) == "visible"
    assert FieldCodec.decode(Annotated[str | None, external], None) is None
    assert FieldCodec.decode(Annotated[str, OrdinaryRepresentation], "visible") == "visible"
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(Annotated[str, OrdinaryRepresentation], {"text": "visible"})


def test_opaque_retains_the_record_while_known_content_projects_declared_fields():
    assert UnknownContent in PiContent.members_with(Opaque)
    assert TextContent not in PiContent.members_with(Opaque)
    original = {"type": "future_content", "nested": {"future": [1, True]}, "text": "extension"}
    assert UnknownContent.normalize_wire(original)["payload"] is original
    unknown = PiContent.from_wire(original)
    assert unknown.payload == original
    assert unknown.to_wire() == original
    known = PiContent.from_wire({"type": "text", "text": "visible", "future": True})
    assert known == TextContent(text="visible")
    assert known.to_wire() == {"type": "text", "text": "visible"}


def test_strict_fields_rejects_extra_keys_while_ordinary_response_projects_them():
    assert SessionSwitchData in PiResponseData.members_with(StrictFields)
    assert StateData not in PiResponseData.members_with(StrictFields)
    assert SessionSwitchData.from_wire({"cancelled": False}) == SessionSwitchData(False)
    with pytest.raises(MalformedRecord):
        SessionSwitchData.from_wire({"cancelled": False, "extra": True})
    assert StateData.from_wire({"extra": True}) == StateData.from_wire({})


@dataclass
class WatchdogSession:
    output: TurnOutput = field(default_factory=lambda: TurnOutput(final_assistant_stop=True))


async def test_accepts_prompt_dispatch_restarts_progress_without_accepting_unowned_events():
    assert AgentStart in PiEvent.members_with(AcceptsPrompt)
    assert QueueUpdate not in PiEvent.members_with(AcceptsPrompt)
    assert IdlePhase in TurnPhase.members_with(AcceptsPrompt)
    watchdog = ProgressWatchdog(ModelWait(), 10, 10, NativeStartupPolicy())
    watchdog.launching(lambda: 3.0, None)
    watchdog.model_wait.elapsed = watchdog.input_wait.elapsed = 2.0
    session = WatchdogSession()
    unowned = PiEvent.from_wire({"type": "queue_update", "pending": 1})
    assert [event async for event in watchdog.observe(unowned, session)] == []
    assert not watchdog.prompt_accepted
    assert session.output.final_assistant_stop
    assert watchdog.model_wait.elapsed == watchdog.input_wait.elapsed == 2.0
    accepted = PiEvent.from_wire({"type": "agent_start"})
    assert [event async for event in watchdog.observe(accepted, session)] == []
    assert watchdog.prompt_accepted
    assert not session.output.final_assistant_stop
    assert watchdog.model_wait.elapsed == watchdog.input_wait.elapsed == 0.0
    assert isinstance(watchdog.phase, ModelWaitPhase)


@pytest.mark.parametrize("family, record", [
    (AssistantBlock, {"type": "text", "text": "visible"}),
    (ResultPart, {"type": "text", "text": "visible"}),
    (UserBlock, {"type": "text", "text": "visible"}),
    (UserInput, {"type": "text", "text": "visible"}),
    (Part, {"type": "text", "text": "visible", "id": "part", "messageID": "message", "sessionID": "session"}),
])
def test_text_wire_is_inherited_by_real_external_part_decoders(family, record):
    adapter = TypeAdapter(family.declared_union())
    decoded = adapter.validate_python(record)
    assert isinstance(decoded, TextWire)
    assert decoded.text == "visible"
    assert decoded.model_dump()["type"] == record["type"]
    assert any(not issubclass(member, TextWire)
               for member in family.__registry_config__.registry_dict.values())
    with pytest.raises(ValueError):
        adapter.validate_python({**record, "type": "unrecognized"})
