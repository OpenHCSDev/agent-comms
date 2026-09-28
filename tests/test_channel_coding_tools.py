"""Normal coding policies use existing claims; no provider or user work is touched."""

import json
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.channel_coding_tools import CodingCall, CodingTool, CodingToolSocket
from agent_comms.comms import Comms
from agent_comms.declarations import Thread
from agent_comms.envelope_claim_transitions import (
    ClaimConflict,
    ClaimTransitionError,
    WritableFileClaim,
    normalize_existing_file,
)


def test_create_claim_competes_before_file_exists_and_releases_without_creation():
    with TemporaryDirectory(prefix="comms-create-claim-", dir="/var/tmp") as directory:
        root = Path(directory) / "wire"
        root.mkdir(mode=0o700)
        work = Path(directory) / "work"
        work.mkdir()
        c = Comms(root)
        for name, created in [("a", 21.0), ("b", 22.0)]:
            c.threads.register(Thread(name, frozenset({"team"}), str(work), created_at=created))
        c.messaging.initialize_private_initial_protocol()
        c.messaging.initialize_private_claim_protocol()
        resource = WritableFileClaim("new/nested/file.py")
        committed = c.messaging.send_message("a", "#team", "Claim before create", claims=[resource])
        assert not (work / "new").exists()
        projection = Comms(root).bus.claim_projection()
        assert projection[str(work / "new/nested/file.py")].seq == committed.seq
        with pytest.raises(ClaimConflict):
            c.messaging.send_message("b", "#team", "Competing create", claims=[resource])
        c.messaging.send_message("a", "#team", "Release unused creation", releases=["new/nested/file.py"])
        c.messaging.send_message("b", "#team", "New owner", claims=[resource])
        assert c.bus.claim_projection()[str(work / "new/nested/file.py")].owner == "b"


def test_create_claim_preserves_physical_worktree_scope(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "work"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/x", "link/missing.py", str(outside / "x"), "."):
        with pytest.raises(ClaimTransitionError):
            WritableFileClaim(path).normalized(root)
    with pytest.raises(ClaimTransitionError):
        normalize_existing_file(root, "new.py")
    (root / "existing").write_text("a")
    (root / "alias").hardlink_to(root / "existing")
    with pytest.raises(ClaimTransitionError):
        WritableFileClaim("existing").normalized(root)


def test_coding_names_and_argument_types_come_from_declarations():
    assert set(CodingTool.names()) == {"read", "bash", "edit", "write"}
    call = CodingCall.decode("call", "write", {"path": "x", "content": "value"})
    assert isinstance(call.tool.resource_claim(), WritableFileClaim)
    with pytest.raises((TypeError, ValueError)):
        CodingCall.decode("call", "write", {"path": True, "content": "v"})
    # Pi's full tool schema stays authoritative, including its current edits[]
    # representation; only the mutation path is decoded by the claim policy.
    args = {"path": "x", "edits": [{"oldText": "a", "newText": "b"}]}
    assert CodingCall.decode("edit", "edit", args).tool.arguments == args
    with pytest.raises(ValueError):
        CodingCall.decode("call", "forged", {})


@pytest.mark.asyncio
async def test_multicall_native_transport_matches_events_and_refuses_duplicate(tmp_path):
    class Owner:
        input_id = secrets.token_hex(16)
        calls = []

        def admit(self, call):
            self.calls.append(call)

    owner = Owner()
    tmp_path.chmod(0o700)
    socket = CodingToolSocket(tmp_path, secrets.token_hex(32), owner)
    # Actual owner admission/storage is covered separately. This exercises the
    # strict native-event/socket correlation and multiple normal tool calls.
    calls = [
        ("call_one|provider-part", "read", {"path": "a"}),
        ("two", "write", {"path": "b", "content": "c"}),
    ]
    socket.announce(
        [
            {"type": "toolCall", "id": id, "name": name, "arguments": args}
            for id, name, args in calls
        ]
    )
    for id, name, args in calls:
        socket.tool_started({"toolCallId": id, "toolName": name, "args": args})
        raw = (
            json.dumps({"token": socket.token, "call_id": id, "name": name, "arguments": args})
            + "\n"
        ).encode()
        assert await socket.handle_request(raw) == {"ok": True}
        with pytest.raises(ValueError, match="already consumed"):
            await socket.handle_request(raw)
        # Terminal ledger needs the consumed slot an actual owner writes.
        from agent_comms.selected_tool_broker import consume_selected_slot

        call = socket.announced[id]
        consume_selected_slot(tmp_path, call.slot(owner.input_id), call.slot(owner.input_id))
        socket.tool_finished({"toolCallId": id, "toolName": name, "isError": False}, owner.input_id)
    socket.assert_complete()
    assert len(owner.calls) == 2
