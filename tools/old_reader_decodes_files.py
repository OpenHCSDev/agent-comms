"""Prove that a reader installed before message files decodes what this checkout publishes.

Run with this checkout's source on the path and the older interpreter as the argument:

    PYTHONPATH=src python tools/old_reader_decodes_files.py \
        "$(head -1 ~/.local/bin/toad | cut -c3-)"

This checkout builds a scratch bus, sends a human DM and an agent channel message that carry
files, and a send with none. It then encodes what an older client reads: the DM's
conversation page, the channel's history page, the recipient's inbox and the file-free send
request. The older interpreter decodes each one with its own installed agent_comms (no
PYTHONPATH) and prints the bodies it read. The script exits non-zero unless every decode
succeeds, keeps every body, and ran on an agent_comms outside this checkout.
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
from agent_comms.registry.registry_store import RegistryStore
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
import agent_comms
from agent_comms.messaging.history_result import ChannelHistoryPageResult
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.transport.tools.inbox import CommsInboxTool
from agent_comms.transport.tools.sidebar import CommsConversationPageTool, CommsUserSendTool
page, history, inbox, request = json.load(sys.stdin)
print(json.dumps([
    agent_comms.__file__,
    [m.body for m in FieldCodec.decode(CommsConversationPageTool.result_type(), page).messages],
    [m.body for m in FieldCodec.decode(ChannelHistoryPageResult, history).messages],
    [m.body for m in FieldCodec.decode(CommsInboxTool.result_type(), inbox).messages],
    FieldCodec.decode(CommsUserSendTool, request).body,
]))
"""

EXPECTED = (
    ["a DM with a file", "a DM without one"],
    ["a channel message with a file"],
    ["a DM with a file", "a DM without one"],
    "no files",
)


def upload(name: str, content: bytes, mime_type: str) -> FileUpload:
    return FileUpload(name, mime_type, base64.b64encode(content).decode("ascii"))


def published(root: Path) -> list[object]:
    """The page, history, inbox and file-free request an older client reads, encoded."""
    Bus.install(Bus.path_in(root))
    StorageRule(floor_gib=0).prepare_isolated(root)
    RegistryStore.at(root).prepare_native_root()
    comms = wire(root)
    comms.registry.declare(NativeThread("recipient", frozenset({"general"}), Checkout(root)))
    human, recipient = HumanCaller(), ThreadCaller("recipient")
    image = upload("shot.png", os.urandom(4096), "image/png")
    human.invoke(
        ServedCommsUserSendTool(target="recipient", body="a DM with a file", files=(image,)),
        comms,
    )
    human.invoke(ServedCommsUserSendTool(target="recipient", body="a DM without one"), comms)
    log = upload("log.txt", b"line\n", "text/plain")
    recipient.invoke(
        CommsSendTool(target="#general", body="a channel message with a file", files=(log,)),
        comms,
    )
    page = human.invoke(
        ServedCommsConversationPageTool(
            conversation=CommandTarget.named("recipient"), project=Checkout(root)
        ),
        comms,
    )
    history = CommsHistoryTool(peer="#general").apply(comms, recipient)
    inbox = CommsInboxTool(thread="recipient").apply(comms, recipient)
    request = CommsUserSendTool(target="recipient", body="no files")
    return [FieldCodec.encode(record) for record in (page, history, inbox, request)]


def main(older: str) -> int:
    with tempfile.TemporaryDirectory() as scratch:
        records = published(Path(scratch) / "bus")
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    decoded = subprocess.run(
        [older, "-c", DECODE],
        input=json.dumps(records),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        cwd="/",
    )
    sys.stdout.write(decoded.stdout)
    sys.stderr.write(decoded.stderr)
    if decoded.returncode:
        return decoded.returncode
    installed, *bodies = json.loads(decoded.stdout)
    if tuple(bodies) != EXPECTED:
        sys.stderr.write(f"decoded {bodies}, expected {list(EXPECTED)}\n")
        return 1
    if Path(installed).is_relative_to(Path(__file__).resolve().parents[1]):
        sys.stderr.write("the older interpreter decoded with this checkout, not its own install\n")
        return 1
    print("older reader decoded every record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
