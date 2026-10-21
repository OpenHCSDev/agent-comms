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
    ExternalJsonShape,
    FieldCodec,
    FieldRepresentation,
    Id128Text,
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
    PiPayload,
    PiResponseData,
    SessionSwitchData,
    StateData,
    StrictFields,
    TextContent,
    UnknownContent,
    UnknownData,
    UnknownPayload,
)
from agent_comms.pi_vocabulary import CompactionReason, PiStopReason
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
    assert UnknownContent.from_wire(original).payload == original
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
    (PiEvent, {"type": "future_notice", "extra": [1]}),
])
def test_open_payload_derives_unknown_selection_from_each_family(family, wire):
    unknown, = family.members_with(UnknownPayload)
    decoded = family.from_wire(wire)
    assert type(decoded) is unknown
    assert decoded.payload == wire
    assert decoded.to_wire() == wire
    assert unknown.from_wire(wire) == decoded


def test_opaque_entry_and_tool_data_keep_the_original_external_shape():
    from agent_comms.native_entries import NativeEntry

    for family, wire in (
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


def test_external_fields_use_their_declared_type_not_their_parent_or_field_name():
    from agent_comms.pi_payloads import (
        AbsentMessage,
        MissingToolResult,
        NoToolDetails,
        PiModel,
        PiPayload,
        PiToolResult,
        UnreportedModel,
    )
    from agent_comms.pi_vocabulary import (
        CompactionReason,
        OverflowCompactionReason,
        PiStopReason,
        StopStopReason,
    )

    @dataclass(frozen=True)
    class RenamedFields(PiPayload):
        notice: PiMessage
        ending: type[PiStopReason]
        cause: type[CompactionReason]
        result: PiToolResult
        extra: NativeToolDetails
        selected: PiModel
        history: tuple[PiMessage, ...]
        optional: PiMessage | None

    decoded = RenamedFields.from_wire({
        "notice": None, "ending": "stop", "cause": "overflow", "result": None,
        "extra": None, "selected": None, "history": [{"role": "user", "content": "hi"}],
        "optional": None,
    })
    assert decoded.notice == AbsentMessage()
    assert decoded.ending is StopStopReason
    assert decoded.cause is OverflowCompactionReason
    assert decoded.result == MissingToolResult()
    assert decoded.extra == NoToolDetails()
    assert decoded.selected == UnreportedModel()
    assert decoded.history[0].text == "hi"
    assert decoded.optional is None


@pytest.mark.parametrize("data", [[{"role": "user"}], [{"role": "user"}, 1, 2]])
def test_external_fixed_tuples_refuse_wrong_lengths_at_the_codec_boundary(data):
    with pytest.raises(MalformedRecord):
        FieldCodec.decode_external(tuple[PiMessage, int], data, {})


@pytest.mark.parametrize("family, spelling", [(PiStopReason, "stop"), (CompactionReason, "overflow")])
def test_external_name_families_derive_one_default_without_accepting_invalid_values(family, spelling):
    from agent_comms.field_codec import DefaultExternalName
    known = family.member_named(spelling)
    fallback, = family.members_with(DefaultExternalName)
    assert family.from_external(spelling) is known
    for value in (None, "future_value"):
        assert family.from_external(value) is fallback
    with pytest.raises(ValueError):
        known.from_external("future_value")
    for value in (True, 5, object()):
        with pytest.raises(MalformedRecord):
            family.from_external(value)


@pytest.mark.parametrize("value", [None, False, "3", {}, 1.5, 0, -1, 2])
def test_measured_totals_share_one_declared_integer_policy(value):
    from agent_comms.pi_payloads import PiContextUsage, PiUsage

    totals = (PiUsage.from_wire({"totalTokens": value}).total_tokens,
              PiContextUsage.from_wire({"tokens": value}).tokens)
    assert [FieldCodec.encode(total) for total in totals] == ([2, 2] if value == 2 else [None, None])


@pytest.mark.parametrize("value", [False, "3", {}, 1.5])
def test_retained_totals_reject_values_only_external_ingress_may_treat_as_unreported(value):
    from agent_comms.pi_payloads import PiContextUsage, PiUsage

    for family, key in ((PiUsage, "totalTokens"), (PiContextUsage, "tokens")):
        with pytest.raises(MalformedRecord):
            FieldCodec.decode(family, {key: value})


@pytest.mark.parametrize("command", ["switch_session", "watch", "compact_durable"])
def test_exact_response_capability_covers_sdk_and_durable_commands(command):
    from agent_comms import pi_durable_backend  # Registers the actual host command family.
    from agent_comms.pi_events import Response

    wire = {"id": "proof", "type": "response", "command": command,
            "success": False, "error": "failed"}
    assert Response.from_wire(wire).error.value_or("") == "failed"
    with pytest.raises(MalformedRecord):
        Response.from_wire({**wire, "extra": True})


def test_strict_fields_rejects_extra_keys_while_ordinary_response_projects_them():
    assert SessionSwitchData in PiResponseData.members_with(StrictFields)
    assert StateData not in PiResponseData.members_with(StrictFields)
    assert SessionSwitchData.from_wire({"cancelled": False}) == SessionSwitchData(False)
    with pytest.raises(MalformedRecord):
        SessionSwitchData.from_wire({"cancelled": False, "extra": True})
    assert StateData.from_wire({"extra": True}) == StateData.from_wire({})


def test_external_null_members_and_nullable_names_use_their_declarations():
    from agent_comms.field_codec import ExternalNullMember
    from agent_comms.pi_payloads import PiModel, PiToolResult

    for family in (PiMessage, PiModel, PiResponseData, PiToolResult, NativeToolDetails):
        member, = family.members_with(ExternalNullMember)
        assert family.from_wire(None) == member()

    @dataclass(frozen=True)
    class NullableName(PiPayload):
        ending: type[PiStopReason] | None

    assert NullableName.from_wire({"ending": None}).ending is None
    known = PiStopReason.member_named("stop")
    assert NullableName.from_wire({"ending": "stop"}).ending is known
    resolved = FieldCodec.decode_external(type[PiStopReason], "stop", {})
    assert FieldCodec.decode_resolved(type[PiStopReason], resolved) is known
    with pytest.raises(MalformedRecord):
        FieldCodec.decode_resolved(type[PiStopReason], StateData)
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(type[PiStopReason], known)


@pytest.mark.parametrize("annotation, wire", [
    (list[PiMessage], [{"role": "future_message", "extra": 1}]),
    (tuple[PiMessage, ...], [{"role": "future_message", "extra": 1}]),
    (tuple[PiMessage, int], [{"role": "future_message", "extra": 1}, 7]),
    (dict[str, PiMessage], {"message": {"role": "future_message", "extra": 1}}),
    (frozenset[type[PiStopReason]], ["stop", "stop"]),
])
def test_external_collections_produce_resolved_members(annotation, wire):
    resolved = FieldCodec.decode_external(annotation, wire, {})
    assert FieldCodec.decode_resolved(annotation, resolved) is resolved
    assert FieldCodec.decode(annotation, FieldCodec.encode(resolved, annotation)) == resolved


def test_response_command_owns_data_decoding_without_redecoding_its_identity():
    from agent_comms.pi_commands import PiCommand
    from agent_comms.pi_events import Response

    seen = []

    class ObservedReply(PiCommand):
        response_payload = StateData

        @classmethod
        def decode_response_data(cls, value):
            seen.append(value)
            return super().decode_response_data(value)

    payload = {"model": None}
    response = Response.from_wire({
        "type": "response", "command": ObservedReply.declared_name,
        "data": payload, "success": True,
    })
    assert response.command is ObservedReply
    assert response.data == StateData.from_wire(payload)
    assert seen == [payload]

    class InvalidResolvedReply(PiCommand):
        @classmethod
        def decode_response_data(cls, value):
            return object()

    with pytest.raises(MalformedRecord):
        Response.from_wire({"type": "response", "command": InvalidResolvedReply.declared_name})


@pytest.mark.parametrize("command", [None, False, 1, {}])
def test_malformed_response_commands_are_not_unknown_commands(command):
    from agent_comms.pi_events import Response

    with pytest.raises(MalformedRecord):
        Response.from_wire({"type": "response", "command": command})


def test_union_container_shape_does_not_skip_member_validation_and_normalization():
    @dataclass(frozen=True)
    class ContainerOrMessage(PiPayload):
        selected: dict[str, int] | PiMessage

    message = ContainerOrMessage.from_wire({"selected": {"role": "user", "content": "hi"}})
    assert isinstance(message.selected, PiMessage)
    assert message.selected.text == "hi"
    assert ContainerOrMessage.from_wire({"selected": {"items": 3}}).selected == {"items": 3}


@pytest.mark.parametrize("wire", [
    {"type": "text_start", "contentIndex": 0},
    {"type": "text_delta", "delta": "native text"},
    {"type": "text_end", "contentIndex": 0},
])
def test_native_delta_records_are_not_normalized_as_the_null_union_member(wire):
    event = PiEvent.from_wire({"type": "message_update", "assistantMessageEvent": wire})
    assert event.assistant_message_event.to_wire() == wire


def test_exact_envelope_derives_additional_response_fields():
    from agent_comms.pi_events import Response

    @dataclass(frozen=True, kw_only=True)
    class HeaderProofResponse(Response):
        receipt: str = ""

    wire = {"type": HeaderProofResponse.declared_name, "command": "switch_session",
            "id": "proof", "success": False, "error": "failed", "receipt": "seen"}
    assert HeaderProofResponse.from_wire(wire).receipt == "seen"
    del wire["receipt"]
    with pytest.raises(MalformedRecord):
        HeaderProofResponse.from_wire(wire)


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
