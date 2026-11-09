"""Native edit evidence survives RPC, ACP, paging and transcript serialization."""

import json

import pytest

from agent_comms.execution.native_entries import TranscriptProjection
from agent_comms.execution.tool_results import (
    NoSentMessage,
    NoToolDiff,
    ToolDiff,
    tool_result_content,
)
from agent_comms.pi_payloads import PiToolResult
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_declaration import ToolDeclaration
from bus_history import read_history
from conftest import installed_comms, transcript_records

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
        ("edit", result(), False, NoToolDiff()),
        ("bash", result(), True, NoToolDiff()),
        ("edit", {"details": {"patch": 123}}, True, NoToolDiff()),
        ("edit", None, True, NoToolDiff()),
        ("edit", {"details": ["extension-defined", 3]}, True, NoToolDiff()),
    ],
)
def test_native_edit_evidence(name, native, ok, expected):
    assert (
        ToolDeclaration.for_name(name).result_diff(
            PiToolResult.from_wire(native), ok)
        == expected
    )




def test_successful_send_preserves_original_publication_in_live_and_saved_content(tmp_path):
    from agent_comms.pi_payloads import PublishedMessageToolDetails, ToolResultMessage
    from agent_comms.transcript.transcript_events import SentTranscript
    from agent_comms.transport.tools.served import invoke_tool

    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(NativeThread('sender', frozenset(), Checkout(tmp_path)))
    comms.registry.declare(NativeThread('recipient', frozenset(), Checkout(tmp_path)))
    published = invoke_tool(comms, "comms_send", {
        "to": "recipient", "body": "Original private publication"}, caller=ThreadCaller("sender"))
    raw = {"content": [{"type": "text", "text": json.dumps(published)}], "details": published}
    result = PiToolResult.from_wire(raw)
    message, = read_history(comms)
    assert result.sent_message(True) == SentTranscript.from_message(message)
    assert result.sent_message(False) == NoSentMessage()
    saved = ToolResultMessage.from_wire({
        "role": "toolResult", "toolCallId": "original-send", "toolName": "comms_send",
        **raw, "isError": False,
    }).transcript_events(TranscriptProjection(position=0),
                         transcript_records.timestamp.at.timestamp())[0]
    assert saved.sent_message == result.sent_message(True)
    assert tool_result_content("original-send", saved.text, saved.diff, saved.sent_message) == (
        tool_result_content("original-send", result.text(), sent_message=result.sent_message(True)))
    invalid = {**published, "id": "another-message"}
    with pytest.raises(ValueError):
        PublishedMessageToolDetails.from_wire(invalid)
    # Older and unrelated untagged output has no original publication witness.
    assert PiToolResult.from_wire({**raw, "details": {"id": published["id"]}}).sent_message(True) == NoSentMessage()
