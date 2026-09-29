"""Typed Pi boundaries exercised through the actual JSON-line codec and stream."""

import asyncio
import json
import sys
from collections.abc import Mapping

import pytest

from agent_comms import agent_events as ae
from agent_comms.backend import stream_agent_events
from agent_comms.pi_commands import GetState, PiCommand, Prompt
from agent_comms.pi_events import MessageEnd, Response, UnknownPiEvent
from agent_comms.pi_payloads import (
    AssistantMessage,
    StateData,
    TextContent,
    ToolCallContent,
    UnknownData,
)
from agent_comms.pi_rpc import PiRpcChannel


def decode(record):
    return PiRpcChannel.decode_record((json.dumps(record) + "\n").encode(), strict=True)


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


@pytest.mark.parametrize(
    "record",
    [
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text"}]}},
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


@pytest.mark.usefixtures("native_rpc_fixture")
async def test_invalid_queued_command_reports_failure_and_reaps_child(tmp_path):
    child = tmp_path / "pi-stub"
    child.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    child.chmod(0o700)
    queue = asyncio.Queue()
    queue.put_nowait(
        {
            "type": "prompt",
            "message": "see",
            "images": [{"type": "image", "data": "bad", "mimeType": "image/png"}],
        }
    )
    async with asyncio.timeout(4):
        events = [
            event
            async for event in stream_agent_events(
                str(child), [], "task", str(tmp_path), steering_queue=queue, require_input_id=False
            )
        ]
    assert isinstance(events[-1], ae.Done) and not events[-1].ok
    assert any("Invalid queued Pi command" in getattr(event, "text", "") for event in events)
    assert queue.empty()  # no implicit replay


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
