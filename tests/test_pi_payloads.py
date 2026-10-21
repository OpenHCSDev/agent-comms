"""Typed Pi boundaries exercised through the actual JSON-line codec and stream."""

import json
from collections.abc import Mapping

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.tool_results import NoToolDiff
from agent_comms.pi_commands import GetState, PiCommand, Prompt
from agent_comms.pi_counts import PiCount, ReportedCount, UnreportedCount
from agent_comms.pi_events import MessageEnd, MessageUpdate, Response, UnknownPiEvent
from agent_comms.pi_payloads import (
    AssistantMessage,
    MissingData,
    MissingToolResult,
    ProvidedToolResult,
    ReportedModel,
    StateData,
    TextContent,
    ToolCallContent,
    UnknownData,
    UnreportedModel,
    UnreportedUsage,
)
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_usage import UsageAccount
from agent_comms.thread_model import DeclaredModel, UndeclaredModel


def decode(record):
    return PiRpcChannel.decode_record((json.dumps(record) + "\n").encode(), strict=True)


@pytest.mark.parametrize("command", ["switch_session"])
def test_selected_command_decodes_original_native_error_envelope(command):
    # Exact error(id, command, message) shape from the committed native RPC owner.
    event = decode({"id": "original", "type": "response", "command": command,
                    "success": False, "error": "Selected preparation source changed"})
    assert isinstance(event, Response)
    assert event.command is PiCommand.member_named(command)
    assert event.refused and isinstance(event.data, MissingData)
    assert event.rejection_details() == {"command": command, "id": "original",
        "success": False, "error": "Selected preparation source changed"}


@pytest.mark.parametrize("change", [
    {"data": {}}, {"extra": True}, {"success": True}, {"success": 0},
    {"success": None}, {"error": None}, {"error": 1},
])
def test_selected_error_envelope_rejects_mixed_or_untyped_fields(change):
    row = {"id": "original", "type": "response", "command": "switch_session",
           "success": False, "error": "Native refusal"}
    with pytest.raises(ValueError):
        decode({**row, **change})


def test_known_nested_payload_has_one_typed_authority():
    event = decode(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "done"},
                    {"type": "toolCall", "id": "call", "name": "read", "arguments": {"path": "a"}},
                ],
                "usage": {"input": 7, "output": 2, "totalTokens": 9},
                "stopReason": "toolUse",
                "providerExtension": "ignored",
            },
        }
    )
    assert isinstance(event, MessageEnd)
    assert isinstance(event.message, AssistantMessage)
    assert event.message.content == (
        TextContent(text="done"),
        ToolCallContent(id="call", name="read", arguments={"path": "a"}),
    )
    assert event.message.tool_calls == (
        ToolCallContent(id="call", name="read", arguments={"path": "a"}),
    )
    assert event.message.usage.total_tokens == PiCount.from_wire(9)
    assert event.message.stop_reason.tool_round
    assert not isinstance(event, Mapping)
    assert not hasattr(event, "wire") and not hasattr(event.message, "wire")


def test_command_owns_response_schema_and_unknown_data_stays_opaque():
    event = decode(
        {
            "type": "response",
            "command": "get_state",
            "data": {
                "sessionId": "sid",
                "model": {"provider": "p", "id": "m", "contextWindow": 1000},
                "nativeInputProofCapability": "pi-native-input-v1-live-only",
            },
        }
    )
    assert isinstance(event, Response) and event.command is GetState
    assert isinstance(event.data, StateData)
    assert event.data.model.context_window == ReportedCount(1000)
    unknown = decode({"type": "future_notice", "nested": [True, 7]})
    assert isinstance(unknown, UnknownPiEvent)
    assert unknown.payload == {"type": "future_notice", "nested": [True, 7]}
    response = decode({"type": "response", "command": "future_command", "data": [7, "opaque"]})
    assert isinstance(response.data, UnknownData) and response.data.payload == [7, "opaque"]


def test_optional_native_observations_have_named_absence_and_one_serialization():
    ack = decode({"type": "response", "command": "prompt", "success": True})
    assert isinstance(ack.data, MissingData)
    assert not ack.data.session_busy
    with pytest.raises(ValueError):
        ack.data.require_payload()
    for state_wire in ({}, {"model": None}):
        state = StateData.from_wire(state_wire)
        assert isinstance(state.model, UnreportedModel)
        assert state.model.thread_model() == UndeclaredModel()
        with pytest.raises(ValueError):
            state.model.require_selection(UndeclaredModel())
        assert FieldCodec.decode(StateData, FieldCodec.encode(state)) == state
        assert "model" not in state.to_wire()
        assert StateData.from_wire(state.to_wire()) == state
    empty_reported = StateData.from_wire({"model": {}})
    assert isinstance(empty_reported.model, ReportedModel)
    assert "model" in empty_reported.to_wire()
    assert StateData.from_wire(empty_reported.to_wire()) == empty_reported
    reported = StateData.from_wire({"model": {"provider": "p", "id": "m", "contextWindow": 1024}})
    assert isinstance(reported.model, ReportedModel)
    assert reported.model.thread_model() == DeclaredModel("p/m")
    assert reported.model.require_selection(DeclaredModel("p/m")) is reported.model
    with pytest.raises(ValueError):
        reported.model.require_selection(DeclaredModel("p/other"))
    for result_wire in ({}, {"result": None}, {"result": {"content": []}}):
        event = decode({"type": "tool_execution_end", **result_wire})
        assert isinstance(event.result, ProvidedToolResult if "result" in result_wire and result_wire["result"] == {"content": []} else MissingToolResult)
        assert event.result.text() == ""
        assert event.result.edit_diff(True) == NoToolDiff()
        assert FieldCodec.decode(type(event), FieldCodec.encode(event)) == event


def test_tracked_commit_observations_preserve_original_receipt_and_refuse_repetition():
    from agent_comms.native_failures import NativePiUnavailable
    from agent_comms.tracked_turn import ObservedNativeCommit, PendingNativeCommit

    event = decode({"type": "input_committed", "inputId": "a" * 32, "sessionId": "session", "sessionEntryId": "entry", "sessionEntryStart": 128})
    pending = PendingNativeCommit()
    with pytest.raises(NativePiUnavailable):
        pending.require()
    observed = pending.capture(event)
    assert isinstance(observed, ObservedNativeCommit)
    assert observed.require() is event
    with pytest.raises(NativePiUnavailable):
        observed.capture(event)




@pytest.mark.parametrize(
    "record",
    [
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text"}]}},
        {"type": "message_end", "message": {"role": "assistant", "content": None}},
        {"type": "message_end", "message": {"role": "assistant", "content": "invalid"}},
        {"type": "message_end", "message": {"role": "assistant"}},
        {"type": "response", "command": "get_state", "data": "invalid"},
        {"type": "message_start", "message": {"role": "user", "inputId": 42}},
        {"type": "context_committed", "requestGeneration": True},
        {"type": "tool_execution_end", "isError": "false"},
    ],
)
def test_malformed_known_authority_fields_fail_at_boundary(record):
    with pytest.raises((ValueError, TypeError)):
        decode(record)


def test_image_forwarding_reuses_image_owner_and_preserves_native_shape():
    record = {
        "type": "prompt",
        "message": "see",
        "ownerTools": {"path": "/unused/test-tool-socket", "token": "test-token"},
        "images": [{"type": "image", "data": "YWJj", "mimeType": "image/png"}],
    }
    command = PiCommand.from_wire(record)
    assert isinstance(command, Prompt) and command.images[0].size == 3
    assert command.to_rpc() == record


@pytest.mark.parametrize(
    "content, expected",
    [
        ([{"type": "thinking", "thinking": "private reasoning"},
          {"type": "text", "text": "first"}, {"type": "text", "text": " second"}],
         "first second"),
        ([{"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": "a"}}],
         None),
        ([{"type": "extension_final", "text": "unattested"}], None),
    ],
)
def test_final_content_owns_native_text_claim(content, expected):
    from agent_comms.native_failures import NativePiUnavailable

    event = decode({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop", "content": content,
    }})
    if expected is None:
        with pytest.raises(NativePiUnavailable):
            _ = event.message.authoritative_text
    else:
        assert event.message.authoritative_text == expected


@pytest.mark.parametrize(
    "stop, content, error, failed",
    [
        ("error", [], "Estimated input leaves no admissible generation budget", True),
        # A transport failure after partial output is still a failed turn.
        ("error", [{"type": "text", "text": '{"decision":"FULL"}'}], "WebSocket error", True),
        ("aborted", [], "aborted", True),
        ("length", [{"type": "text", "text": "cut"}], None, True),
        ("stop", [{"type": "text", "text": "done"}], None, False),
        # A saved tool round cannot show whether the live owner accepted it.
        ("toolUse", [], None, False),
    ],
)
def test_saved_failed_turn_uses_the_live_turn_end_decision(stop, content, error, failed):
    from agent_comms.native_entries import NativeEntry

    message = dict(role="assistant", content=content, stopReason=stop,
                   api="api", provider="provider", model="model")
    if error is not None:
        message["errorMessage"] = error
    entry = NativeEntry.from_evidence(dict(
        type="message", id="assistant", parentId="user",
        timestamp="2026-10-10T00:00:00Z", message=message,
    ))
    if failed:
        entry.require_failed_terminal("user")
    else:
        with pytest.raises(ValueError):
            entry.require_failed_terminal("user")


def test_answer_given_before_a_later_input_stands_when_that_input_ends_without_text():
    from agent_comms.tracked_turn import AnsweredTrackedTerminal, PendingTrackedTerminal

    answered = PendingTrackedTerminal().append("first answer").later_input(["first ", "answer"])

    assert isinstance(answered, AnsweredTrackedTerminal)
    assert answered.tool_round().append("").require_response([]) == "first answer"
    assert answered.tool_round().append("second answer").require_response(["second answer"]) == "second answer"
    assert isinstance(PendingTrackedTerminal().tool_round().later_input([]), PendingTrackedTerminal)
    unchecked = PendingTrackedTerminal().append("first answer").later_input(["different stream"])
    assert isinstance(unchecked, PendingTrackedTerminal)


def test_pi_usage_and_its_token_total_are_reported_or_not_once_decoded():
    for usage in (None, {"totalTokens": 0}, {"totalTokens": 7}):
        message = {"role": "assistant", "content": [], **({} if usage is None else {"usage": usage})}
        event = decode({"type": "message_update", "message": message, "usage": None})
        assert isinstance(event, MessageUpdate) and isinstance(event.usage, UnreportedUsage)
        account = UsageAccount()
        event.message.measured_tokens.confirm(account)
        charged = event.message.usage.charges(account)
        if usage is None:
            assert isinstance(event.message.usage, UnreportedUsage) and charged == ()
        else:
            assert [charge.usage for charge in charged] == [event.message.usage]
        if usage == {"totalTokens": 7}:
            assert event.message.measured_tokens == ReportedCount(7) and account.confirmed == PiCount.from_wire(7)
        else:
            assert event.message.measured_tokens == UnreportedCount() and account.confirmed == PiCount.from_wire(None)
        assert "usage" not in event.to_wire() and decode(event.to_wire()).usage == event.usage


def test_raw_tool_arguments_reach_the_declared_call_refusal():
    from agent_comms.native_tool_call import CodingCall, RefusedCodingCall
    from agent_comms.native_tool_call import SelectedToolDenied

    event = decode({"type": "message_end", "message": {
        "role": "assistant", "content": [
            {"type": "toolCall", "id": "c", "name": "read", "arguments": []}],
    }})
    raw, = event.message.tool_calls
    assert raw.arguments == []
    call = CodingCall.decode(raw.id, raw.name, raw.arguments)
    assert isinstance(call, RefusedCodingCall)
    with pytest.raises(SelectedToolDenied):
        call.allowed_claim()


@pytest.mark.parametrize(
    ("line", "member"),
    [
        (b'{"type":"auto_retry_end","success":true,"attempt":1}\n', "succeeded"),
        (
            b'{"type":"auto_retry_end","success":false,"attempt":3,'
            b'"finalError":"overloaded_error"}\n',
            "failed",
        ),
    ],
)
def test_auto_retry_end_decodes_pi_boolean_success_into_its_outcome(line, member):
    """Pi's own auto_retry_end record (agent-session.js) selects the outcome by its boolean."""
    from agent_comms.pi_events import AutoRetryEnd
    from agent_comms.pi_rpc import PiRpcChannel

    event = PiRpcChannel.decode_record(line, strict=True)

    assert isinstance(event, AutoRetryEnd)
    assert event.outcome.declared_name == member
