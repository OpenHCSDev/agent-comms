"""Native edit evidence survives RPC, ACP, paging and transcript serialization."""

import json
import sys

import pytest

from agent_comms import Thread, TranscriptEvent, backend, wire
from agent_comms.acp import CommsAgent
from agent_comms.tool_results import ToolDiff, tool_result_content

PATCH = "--- src/example.py\n+++ src/example.py\n@@ -40,2 +40,2 @@\n context\n-old = 1\n+new = 2\n"


def result(patch=PATCH):
    return {
        "content": [
            {"type": "text", "text": "Successfully replaced 1 block(s) in src/example.py."}
        ],
        "details": {"patch": patch, "diff": "-41 old = 1\n+41 new = 2", "firstChangedLine": 41},
    }


@pytest.mark.parametrize(
    "name, native, ok, expected",
    [
        ("edit", result(), True, ToolDiff(PATCH)),
        (
            "edit",
            {"details": {"diff": "-1 old\n+1 new"}},
            True,
            ToolDiff("-1 old\n+1 new", "numbered"),
        ),
        ("edit", result(), False, None),
        ("bash", result(), True, None),
        ("edit", {"details": {"patch": 123}}, True, None),
        ("edit", None, True, None),
    ],
)
def test_native_edit_evidence(name, native, ok, expected):
    assert ToolDiff.from_result(name, native, ok) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Executable POSIX test stub")
async def test_live_diff_matches_result_only_replay_page(tmp_path):
    # Diffs bypass the ordinary tool-output preview's 4K truncation.
    patch = PATCH + " context\n" * 600
    native = result(patch)
    payloads = [
        {
            "type": "tool_execution_start",
            "toolCallId": "edit/1",
            "toolName": "edit",
            "args": {"path": "src/example.py"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "edit/1",
            "toolName": "edit",
            "result": native,
            "isError": False,
        },
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
        {"type": "agent_settled"},
    ]
    stub = tmp_path / "pi-stub"
    stub.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "send = lambda payload: print(json.dumps(payload), flush=True)\n"
        "state = json.loads(sys.stdin.readline())\n"
        "send({'type': 'response', 'command': 'get_state', 'id': state['id'], "
        "'success': True, 'data': {'nativeInputProofCapability': "
        "'pi-native-input-v1-live-only'}})\n"
        "prompt = json.loads(sys.stdin.readline())\n"
        "send({'type': 'response', 'command': 'prompt', 'id': prompt['id'], 'success': True})\n"
        "send({'type': 'message_start', 'message': {'role': 'user', "
        "'content': prompt['message'], 'inputId': prompt['inputId']}})\n"
        f"for payload in {payloads!r}: send(payload)\n"
        "sys.stdin.readline()  # postturn get_state\n"
        "sys.stdin.readline()  # get_session_stats\n"
        "send({'type': 'response', 'command': 'get_session_stats', 'success': True, "
        "'data': {'contextUsage': {'tokens': 33}}})\n"
    )
    stub.chmod(0o755)
    events = [
        event async for event in backend.stream_agent_events(str(stub), [], "task", str(tmp_path))
    ]
    live = next(event for event in events if event["type"] == "tool_end")
    assert live["diff"].text == patch

    session = tmp_path / "session.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolName": "edit",
                    "toolCallId": "edit/1",
                    "isError": False,
                    **native,
                },
            }
        )
        + "\n"
    )
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    page = comms.thread_transcript_page("worker", max_messages=1)
    assert len(page.events) == 1
    saved = TranscriptEvent.from_wire(page.events[0].to_wire())
    assert saved.diff == live["diff"]

    class Client:
        transcript_snapshots = False
        transcript_diffs = False

        def __init__(self):
            self.updates = []

        async def session_update(self, **kwargs):
            self.updates.append(kwargs["update"].model_dump(by_alias=True, exclude_none=True))

    agent = CommsAgent(comms)
    client = Client()
    await agent._emit_event("worker", live, client)
    live_content = client.updates[-1]["content"]
    resource = live_content[0]["content"]["resource"]
    assert resource["mimeType"] == "text/x-diff"
    assert resource["text"] == patch
    assert resource["uri"].endswith("edit%2F1")
    await agent._replay_transcript("worker", "worker", client)
    assert client.updates[-1]["content"] == live_content
    # A newer owner must not send extra TranscriptEvent fields to an old UI.
    client.transcript_snapshots = True
    await agent._replay_transcript("worker", "worker", client)
    snapshot = client.updates[-1]["_meta"]["agentComms"]["transcript"]
    assert "diff" not in snapshot[0]
    client.transcript_diffs = True
    await agent._replay_transcript("worker", "worker", client)
    snapshot = client.updates[-1]["_meta"]["agentComms"]["transcript"]
    assert TranscriptEvent.from_wire(snapshot[0]).diff == live["diff"]
    client.transcript_snapshots = False
    client.transcript_diffs = False
    await agent._replay_transcript("worker", "worker", client, snapshots=True)
    snapshot = client.updates[-1]["_meta"]["agentComms"]["transcript"]
    assert "diff" not in snapshot[0]
    await agent._replay_transcript("worker", "worker", client, snapshots=True, diffs=True)
    snapshot = client.updates[-1]["_meta"]["agentComms"]["transcript"]
    assert TranscriptEvent.from_wire(snapshot[0]).diff == live["diff"]


def test_older_transcript_wire_payload_has_no_diff():
    event = TranscriptEvent.from_wire({"kind": "tool_end", "text": "old saved output"})
    assert event.diff is None
    assert tool_result_content("old", event.text, event.diff) == [
        {"type": "content", "content": {"type": "text", "text": "old saved output"}}
    ]
