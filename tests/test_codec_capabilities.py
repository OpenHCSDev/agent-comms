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
    BoundedText,
    EnvelopeSequence,
    Id128Text,
    ExternalJsonShape,
    FieldCodec,
    FieldRepresentation,
    MalformedRecord,
)
from agent_comms.native_startup import NativeStartupPolicy
from agent_comms.pi_events import AgentStart, PiEvent, QueueUpdate, RetryProgress
from agent_comms.pi_payloads import (
    ContentDelta,
    NativeToolDetails,
    Opaque,
    PiContent,
    PiDelta,
    PiDiagnostic,
    PiMessage,
    PiResponseData,
    SessionSwitchData,
    StateData,
    StrictFields,
    TextContent,
    UnknownContent,
    UnknownData,
    UnknownPayload,
)
from agent_comms.turn_output import TurnOutput
from agent_comms.turn_phase import AcceptsPrompt, ModelWaitPhase
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


@pytest.mark.parametrize("family, wire", [
    (PiContent, {"type": "future_content", "extra": [1]}),
    (PiDiagnostic, {"type": "future_diagnostic", "extra": [1]}),
    (PiMessage, {"role": "future_message", "extra": [1]}),
    (PiDelta, {"type": "future_delta", "extra": [1]}),
])
def test_open_payload_derives_unknown_selection_from_each_family(family, wire):
    unknown, = family.members_with(UnknownPayload)
    decoded = family.from_wire(wire)
    assert type(decoded) is unknown
    assert decoded.payload == wire
    assert decoded.to_wire() == wire
    assert unknown.from_wire(wire) == decoded


def test_opaque_event_entry_and_tool_data_keep_the_original_external_shape():
    from agent_comms.native_entries import NativeEntry

    for family, wire in (
        (PiEvent, {"type": "future_notice", "extra": [1]}),
        (NativeEntry, {"type": "future_entry", "timestamp": "2026-10-21T00:00:00Z", "extra": [1]}),
        (NativeToolDetails, {"extra": [1]}),
        (UnknownData, [1, {"extra": True}]),
    ):
        assert family.from_wire(wire).to_wire() == wire


@pytest.mark.parametrize("invalid", [object(), {1: "not a JSON key"}, float("inf")])
def test_opaque_payload_uses_the_canonical_json_value_boundary(invalid):
    with pytest.raises(MalformedRecord):
        UnknownData.from_wire(invalid)


@pytest.mark.parametrize("representation, valid, invalid", [
    (BoundedText, "visible", ""),
    (EnvelopeSequence, 1, 0),
    (Id128Text, "a" * 32, "not-an-id"),
])
def test_scalar_siblings_validate_identically_on_encode_and_decode(representation, valid, invalid):
    assert representation.encode(valid) == representation.decode(valid) == valid
    for convert in (representation.encode, representation.decode):
        with pytest.raises(MalformedRecord):
            convert(invalid)


async def test_delta_siblings_share_progress_and_inherit_default_emission():
    for member in PiDelta.members_with(ContentDelta):
        assert member(delta="visible").progress
        assert not member(delta="").progress
    for kind in ("toolcall_start", "toolcall_delta", "toolcall_end", "future_delta"):
        delta = PiDelta.from_wire({"type": kind})
        assert await delta.emit(None) == delta.events() == ()
    for member in PiEvent.members_with(RetryProgress):
        assert member().retry_progress
    phase = ModelWaitPhase(detail="retained")
    assert phase.model_progress() is phase


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
