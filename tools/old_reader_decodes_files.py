"""Prove that a reader installed before message files decodes what this checkout publishes.

Run with this checkout's source on the path and the older interpreter as the argument:

    PYTHONPATH=src python tools/old_reader_decodes_files.py "$(head -1 ~/.local/bin/toad | cut -c3-)"

This checkout builds a scratch bus, sends a human DM and an agent channel message that carry
files, and a send with none, then encodes what an older client reads: the DM's conversation
page, the channel's history page, the recipient's inbox and the file-free send request. The
older interpreter decodes each one with its own installed agent_comms (no PYTHONPATH), and
the script exits non-zero unless every decode succeeds and keeps every body.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_files import FileUpload
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tool_caller import HumanCaller, ThreadCaller
from agent_comms.transport.tools.inbox import CommsInboxTool
from agent_comms.transport.tools.send import CommsSendTool
from agent_comms.transport.tools.served import (
    CommsHistoryTool,
    ServedCommsConversationPageTool,
    ServedCommsUserSendTool,
)
from agent_comms.transport.tools.sidebar import CommsUserSendTool

DECODE = """
import json, sys
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.transport.tools.sidebar import CommsConversationPageTool, CommsUserSendTool
from agent_comms.messaging.history_result import ChannelHistoryPageResult
from agent_comms.transport.tools.inbox import CommsInboxTool
import agent_comms
records = json.load(sys.stdin)
page = FieldCodec.decode(CommsConversationPageTool.result_type(), records["page"])
history = FieldCodec.decode(ChannelHistoryPageResult, records["history"])
inbox = FieldCodec.decode(CommsInboxTool.result_type(), records["inbox"])
request = FieldCodec.decode(CommsUserSendTool, records["request"])
print(json.dumps({
    "agent_comms": agent_comms.__file__,
    "page": [message.body for message in page.messages],
    "history": [message.body for message in history.messages],
    "inbox": [message.body for message in inbox.messages],
    "request": request.body,
}))
"""


def upload(name: str, content: bytes, mime_type: str) -> FileUpload:
    return FileUpload(name, mime_type, base64.b64encode(content).decode("ascii"))


def published(root: Path) -> dict[str, object]:
    Bus.install(Bus.path_in(root))
    StorageRule(floor_gib=0).prepare_isolated(root)
    comms = wire(root)
    comms.registry.store.prepare_native_root()
    comms.registry.declare(NativeThread("recipient", frozenset({"general"}), Checkout(root)))
    HumanCaller().invoke(ServedCommsUserSendTool(
        target="recipient", body="a DM with a file",
        files=(upload("shot.png", os.urandom(4096), "image/png"),)), comms)
    HumanCaller().invoke(ServedCommsUserSendTool(target="recipient", body="a DM without one"), comms)
    ThreadCaller("recipient").invoke(CommsSendTool(
        target="#general", body="a channel message with a file",
        files=(upload("log.txt", b"line\n", "text/plain"),)), comms)
    page = HumanCaller().invoke(ServedCommsConversationPageTool(
        conversation=CommandTarget.named("recipient"), project=Checkout(root)), comms)
    history = ThreadCaller("recipient").invoke(CommsHistoryTool(peer="#general"), comms)
    inbox = ThreadCaller("recipient").invoke(CommsInboxTool(thread="recipient"), comms)
    return {
        "page": FieldCodec.encode(page),
        "history": FieldCodec.encode(history),
        "inbox": FieldCodec.encode(inbox),
        "request": FieldCodec.encode(CommsUserSendTool(target="recipient", body="no files")),
    }


def main(older: str) -> int:
    with tempfile.TemporaryDirectory() as scratch:
        records = published(Path(scratch) / "bus")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    decoded = subprocess.run(
        [older, "-c", DECODE], input=json.dumps(records), capture_output=True, text=True,
        env=environment, check=False, cwd="/",
    )
    sys.stdout.write(decoded.stdout)
    sys.stderr.write(decoded.stderr)
    if decoded.returncode:
        return decoded.returncode
    answer = json.loads(decoded.stdout)
    expected = {
        "page": ["a DM with a file", "a DM without one"],
        "history": ["a channel message with a file"],
        "inbox": ["a DM with a file", "a DM without one"],
        "request": "no files",
    }
    mismatched = {key: (answer[key], value) for key, value in expected.items() if answer[key] != value}
    if mismatched:
        sys.stderr.write(f"decoded differently: {mismatched}\n")
        return 1
    if Path(answer["agent_comms"]).is_relative_to(Path(__file__).resolve().parents[1]):
        sys.stderr.write("the older interpreter decoded with this checkout, not its own install\n")
        return 1
    print("older reader decoded every record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
