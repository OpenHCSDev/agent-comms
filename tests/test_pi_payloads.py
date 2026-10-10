"""Typed Pi boundaries exercised through the actual JSON-line codec and stream."""

import json
from types import SimpleNamespace
from collections.abc import Mapping

import pytest

from agent_comms.pi_commands import GetState, PiCommand, Prompt
from agent_comms.pi_events import MessageEnd, Response, UnknownPiEvent
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
)
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_rpc import PiRpcChannel


def decode(record):
    return PiRpcChannel.decode_record((json.dumps(record) + "\n").encode(), strict=True)


@pytest.mark.parametrize("command", [
    "switch_session", "agent_comms_summarize_compaction", "agent_comms_compaction_settings",
    "agent_comms_prepare_compaction", "agent_comms_restore_compaction",
])
def test_selected_command_decodes_original_native_error_envelope(command):
    # Exact error(id, command, message) shape from the committed native RPC owner.
    event = decode({"id": "original", "type": "response", "command": command,
                    "success": False, "error": "Selected preparation source changed"})
    assert isinstance(event, Response)
    assert event.command is PiCommand.decode(command)
    assert event.success is False and isinstance(event.data, MissingData)
    assert event.rejection_details() == {"command": command, "id": "original",
        "success": False, "error": "Selected preparation source changed"}


def test_selected_rejection_keeps_native_reason_without_payload_authority():
    from agent_comms.pi_commands import SwitchSession

    request = SwitchSession(id="original", session_path="/private/session.jsonl")
    event = decode({"id": request.id, "type": "response", "command": "switch_session",
                    "success": False, "error": "Original session is unavailable"})
    with pytest.raises(ValueError, match="Original session is unavailable"):
        event.require_request(request)
    with pytest.raises(ValueError, match="does not match"):
        event.require_request(SwitchSession(id="different", session_path=request.session_path))
    successful = decode({"id": request.id, "type": "response", "command": "switch_session",
                         "success": True, "data": {"cancelled": False}})
    assert successful.require_request(request) is successful.data


@pytest.mark.parametrize("change", [
    {"data": {}}, {"extra": True}, {"success": True}, {"success": 0},
    {"success": None}, {"error": None}, {"error": 1},
])
def test_selected_error_envelope_rejects_mixed_or_untyped_fields(change):
    row = {"id": "original", "type": "response", "command": "agent_comms_prepare_compaction",
           "success": False, "error": "Native refusal"}
    with pytest.raises(ValueError, match="Unexpected selected response envelope"):
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
    assert event.message.usage.total_tokens == 9
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
    assert event.data.model.context_window == 1000
    unknown = decode({"type": "future_notice", "nested": [True, 7]})
    assert isinstance(unknown, UnknownPiEvent)
    assert unknown.payload == {"type": "future_notice", "nested": [True, 7]}
    response = decode({"type": "response", "command": "future_command", "data": [7, "opaque"]})
    assert isinstance(response.data, UnknownData) and response.data.payload == [7, "opaque"]


def test_optional_native_observations_have_named_absence_and_one_serialization():
    ack = decode({"type": "response", "command": "prompt", "success": True})
    assert isinstance(ack.data, MissingData)
    assert not ack.data.session_busy
    with pytest.raises(ValueError, match="no data"):
        ack.data.require_payload()
    for state_wire in ({}, {"model": None}):
        state = StateData.from_wire(state_wire)
        assert isinstance(state.model, UnreportedModel)
        assert not state.matches_model(("p", "m"))
        with pytest.raises(ValueError, match="owner selection"):
            state.model.require_selection(None)
        with pytest.raises(ValueError, match="owner selection"):
            state.model.for_compaction("p/m")
        assert FieldCodec.decode(StateData, FieldCodec.encode(state)) == state
        assert "model" not in state.to_wire()
        assert StateData.from_wire(state.to_wire()) == state
    empty_reported = StateData.from_wire({"model": {}})
    assert isinstance(empty_reported.model, ReportedModel)
    assert "model" in empty_reported.to_wire()
    assert StateData.from_wire(empty_reported.to_wire()) == empty_reported
    reported = StateData.from_wire({"model": {"provider": "p", "id": "m", "contextWindow": 1024}})
    assert isinstance(reported.model, ReportedModel)
    assert reported.matches_model(("p", "m"))
    assert reported.model.require_selection("p/m") is reported.model
    assert reported.model.for_compaction("p/m").context_window == 1024
    with pytest.raises(ValueError, match="owner selection"):
        reported.model.for_compaction("p/other")
    for result_wire in ({}, {"result": None}, {"result": {"content": []}}):
        event = decode({"type": "tool_execution_end", **result_wire})
        assert isinstance(event.result, ProvidedToolResult if "result" in result_wire and result_wire["result"] == {"content": []} else MissingToolResult)
        assert event.result.text() == ""
        assert event.result.edit_diff(True) is None
        assert FieldCodec.decode(type(event), FieldCodec.encode(event)) == event


def test_native_terminal_reason_owns_error_detail_without_a_second_verdict():
    event = decode({
        "type": "message_end",
        "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "original provider rejection"},
    })
    failures = []
    session = SimpleNamespace(fail_terminal=failures.append)
    event.message.tracked_end(session)
    assert failures == ["original provider rejection"]


def test_tracked_commit_observations_preserve_original_receipt_and_refuse_repetition():
    from agent_comms.native_pi import NativePiUnavailable
    from agent_comms.tracked_turn import ObservedNativeCommit, PendingNativeCommit

    event = decode({"type": "input_committed", "inputId": "a" * 32, "sessionId": "session", "sessionEntryId": "entry"})
    pending = PendingNativeCommit()
    assert not pending.observed
    with pytest.raises(NativePiUnavailable, match="tracked model context"):
        pending.require()
    observed = pending.capture(event)
    assert isinstance(observed, ObservedNativeCommit) and observed.observed
    assert observed.require() is event
    with pytest.raises(NativePiUnavailable, match="repeated the input commitment"):
        observed.capture(event)


def test_tracked_terminal_states_preserve_failure_and_unique_stream_relation():
    from pathlib import Path
    from agent_comms.native_pi import NativeContextProof, NativePiTerminalFailure, NativePiUnavailable
    from agent_comms.tracked_turn import AmbiguousTrackedTerminal, PendingTrackedTerminal

    pending = PendingTrackedTerminal()
    with pytest.raises(NativePiUnavailable, match="unique authoritative"):
        pending.require_response([])
    completed = pending.append("actual response")
    assert completed.require_response(["actual ", "response"]) == "actual response"
    with pytest.raises(NativePiUnavailable, match="unique authoritative"):
        completed.require_response(["different stream"])
    ambiguous = completed.append("second terminal")
    assert isinstance(ambiguous, AmbiguousTrackedTerminal)
    with pytest.raises(NativePiUnavailable, match="unique authoritative"):
        ambiguous.require_response(["actual response"])
    assert isinstance(completed.tool_round(), PendingTrackedTerminal)
    failed = completed.fail("original provider rejection")
    assert failed.tool_round() is failed and failed.append("later text") is failed
    proof = NativeContextProof("a" * 32, "session", "entry", 1, "b" * 64, Path("original-session.jsonl"))
    with pytest.raises(NativePiTerminalFailure, match="original provider rejection") as failure:
        failed.raise_failure(proof, "configured", "model")
    assert failure.value.context is proof


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
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "c", "name": "read", "arguments": []}],
            },
        },
    ],
)
def test_malformed_known_authority_fields_fail_at_boundary(record):
    with pytest.raises((ValueError, TypeError)):
        decode(record)


@pytest.mark.parametrize("mutation", ["extra", "kind", "version", "reason", "envelope"])
def test_selected_observations_keep_strict_envelope_and_payload(mutation):
    record = {
        "type": "response",
        "id": "r",
        "command": "agent_comms_summarize_compaction",
        "success": True,
        "data": {
            "version": 1,
            "operationId": "op",
            "status": "unknown",
            "reason": "402: configured provider",
        },
    }
    if mutation == "envelope":
        record["extra"] = 1
    elif mutation == "version":
        record["data"]["version"] = True
    elif mutation == "reason":
        record["data"]["reason"] = "hidden\nline"
    else:
        record["data"][mutation] = "unexpected"
    with pytest.raises((ValueError, TypeError)):
        decode(record)


def test_image_forwarding_reuses_image_owner_and_preserves_native_shape():
    record = {
        "type": "prompt",
        "message": "see",
        "images": [{"type": "image", "data": "YWJj", "mimeType": "image/png"}],
    }
    command = PiCommand.from_wire(record)
    assert isinstance(command, Prompt) and command.images[0].size == 3
    assert command.to_rpc() == record


def test_native_startup_metadata_uses_existing_entry_family():
    from agent_comms.native_entries import (
        ModelChangeEntry,
        NativeEntry,
        StartupMetadataEntry,
        ThinkingLevelChangeEntry,
        UnknownEntry,
    )

    model = {
        "type": "model_change",
        "id": "1234abcd",
        "parentId": None,
        "timestamp": "2026-09-28T12:00:00Z",
        "provider": "test",
        "modelId": "local",
    }
    thinking = {
        "type": "thinking_level_change",
        "id": "abcd1234",
        "parentId": model["id"],
        "timestamp": model["timestamp"],
        "thinkingLevel": "high",
    }
    first = StartupMetadataEntry.read_startup(json.dumps(model).encode())
    second = StartupMetadataEntry.read_startup(json.dumps(thinking).encode())
    assert isinstance(first, ModelChangeEntry)
    assert isinstance(second, ThinkingLevelChangeEntry)
    assert second.parent_id == first.id
    assert first.matches_startup(("test", "local"), "high")
    assert second.matches_startup(("test", "local"), "high")
    assert not first.matches_startup(("test", "other"), "high")
    assert not second.matches_startup(("test", "local"), "low")
    assert NativeEntry.from_wire(model) == first
    assert NativeEntry.from_wire(thinking) == second
    session_parent = "98765432-abcd-4567-8123-0123456789ab"
    rooted = StartupMetadataEntry.read_startup(
        json.dumps({**model, "parentId": session_parent}).encode()
    )
    assert rooted.parent_id == session_parent
    with pytest.raises(ValueError):
        StartupMetadataEntry.read_startup(json.dumps({**model, "id": session_parent}).encode())
    assert isinstance(NativeEntry.from_wire({"type": "future"}), UnknownEntry)
    # Native evidence is fail closed even though history can display opaque entries.
    for row in (model, thinking):
        malformed = [{k: v for k, v in row.items() if k != missing} for missing in row] + [
            {**row, "extra": True},
            {**row, "id": "invalid"},
            {**row, "parentId": 1},
            {**row, "parentId": "invalid"},
            {**row, "timestamp": ""},
            {**row, "timestamp": None},
            {**row, "type": "future"},
            {**row, "type": "compaction"},
        ]
        for bad in malformed:
            with pytest.raises((ValueError, TypeError)):
                StartupMetadataEntry.read_startup(json.dumps(bad).encode())
        duplicate = json.dumps(row)[:-1] + ', "id": "1234abcd"}'
        with pytest.raises(ValueError):
            StartupMetadataEntry.read_startup(duplicate.encode())


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
def test_final_content_owns_native_text_admission(content, expected):
    from agent_comms.native_pi import NativePiUnavailable

    event = decode({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop", "content": content,
    }})
    if expected is None:
        with pytest.raises(NativePiUnavailable, match="non-text content"):
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
        with pytest.raises(ValueError, match="did not end its turn as a failure"):
            entry.require_failed_terminal("user")
