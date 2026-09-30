"""Native edit evidence survives RPC, ACP, paging and transcript serialization."""

import json
import sys

import pytest

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import PiToolResult
from agent_comms.native_tools import NativeTool
from agent_comms.threads import Thread
from agent_comms.tool_results import ToolDiff, tool_result_content
from agent_comms.transcript_events import ToolEndTranscript, TranscriptEvent

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
        ("edit", {"details": ["extension-defined", 3]}, True, None),
    ],
)
def test_native_edit_evidence(name, native, ok, expected):
    assert (
        NativeTool.for_name(name).result_diff(
            PiToolResult.from_wire(native) if native is not None else None, ok)
        == expected
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Executable POSIX test stub")
async def test_live_diff_matches_result_only_replay_page(tmp_path, native_rpc_fixture):
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
    live = next(event for event in events if isinstance(event, ae.ToolEnd))
    assert live.diff.text == patch

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
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    page = comms.transcripts.thread_transcript_page("worker", max_messages=1)
    assert len(page.events) == 1
    saved = FieldCodec.decode(TranscriptEvent, FieldCodec.encode(page.events[0]))
    assert saved.diff == live.diff

    class Client:
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
    await agent.sessions.transcript.replay("worker", "worker", client)
    (snapshot,) = decode_updates(client.updates[-1]["_meta"])
    assert isinstance(snapshot, TranscriptSnapshotUpdate)
    assert snapshot.page.events[0].diff == live.diff


def test_plain_tool_result_keeps_text_content():
    event = ToolEndTranscript(tool_call_id="plain", tool_name="read", text="saved output")
    assert event.diff is None
    assert tool_result_content("plain", event.text, event.diff) == [
        {"type": "content", "content": {"type": "text", "text": "saved output"}}
    ]
