"""Normal coding policies use existing claims; no provider or user work is touched."""

import json
import os
import secrets
from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory

import pytest

from agent_comms.native_tool_call import CodingCall
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.storage_room import NoRoomNeeded
from agent_comms.comms import Comms
from agent_comms.envelope_claim_transitions import (
    ClaimConflict,
    ClaimTransitionError,
    ExistingFileClaim,
    WritableFileClaim,
    NativeClaim,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordination_session import NativeBoundary
from agent_comms.native_tools import CodingTool
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart
from agent_comms.pi_payloads import ToolCallContent
from agent_comms.threads import Thread


def test_create_claim_competes_before_file_exists_and_releases_without_creation():
    with TemporaryDirectory(prefix="comms-create-claim-", dir="/var/tmp") as directory:
        root = Path(directory) / "wire"
        root.mkdir(mode=0o700)
        work = Path(directory) / "work"
        work.mkdir()
        c = Comms(root)
        for name, created in [("a", 21.0), ("b", 22.0)]:
            thread = c.registry.new_thread(name, frozenset({'team'}), str(work), created_at=created)
            c.registry.declare(replace(thread, process_identity=ProcessIdentity.capture(os.getpid())))
        c.messaging.initialize_private_initial_protocol()
        resource = WritableFileClaim(Path("new/nested/file.py"))
        def acquire(name):
            with NativeBoundary(c.bus) as boundary:
                owner = boundary.registry.require(name)
                scope = NativeClaim(owner_claimed_generation=boundary.registry.claimed_generations[name],
                                    operation_id=secrets.token_hex(16), turn_id=f"create-{name}",
                                    input_id=f"create-input-{name}")
                return c.bus.publisher._publish_acquisition_unlocked(boundary.bus_lock,
                    boundary.registry, owner, resource.normalized(work), scope,
                    activity=boundary.activity)
        committed = acquire("a")
        assert not (work / "new").exists()
        assert c.bus.log.claim_projection()[str(work / "new/nested/file.py")].seq == committed.seq
        with pytest.raises(ClaimConflict):
            acquire("b")
        with NativeBoundary(c.bus) as boundary:
            c.bus.publisher.release_resources(boundary, (committed,), "Release unused creation")
        assert acquire("b").owner == "b"


def test_create_claim_preserves_physical_paths_without_aliases(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "work"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/x", "link/missing.py", ".", outside):
        with pytest.raises(ClaimTransitionError):
            WritableFileClaim(Path(path)).normalized(root)
    assert WritableFileClaim(outside / "x").normalized(root) == str(outside / "x")
    with pytest.raises(ClaimTransitionError):
        ExistingFileClaim(Path("new.py")).normalized(root)
    (root / "existing").write_text("a")
    (root / "alias").hardlink_to(root / "existing")
    with pytest.raises(ClaimTransitionError):
        WritableFileClaim(Path("existing")).normalized(root)


def test_coding_names_and_argument_types_come_from_declarations():
    assert set(CodingTool.names()) == {"read", "bash", "edit", "write"}
    call = CodingCall.decode("call", "write", {"path": "x", "content": "value"})
    assert isinstance(call.claim, WritableFileClaim)
    with pytest.raises((TypeError, ValueError)):
        CodingCall.decode("call", "write", {"path": True, "content": "v"}).allowed_claim()
    # Pi's full tool schema stays authoritative, including its current edits[]
    # representation; only the mutation path is decoded by the claim policy.
    args = {"path": "x", "edits": [{"oldText": "a", "newText": "b"}]}
    assert CodingCall.decode("edit", "edit", args).arguments == args
    with pytest.raises(ValueError):
        CodingCall.decode("call", "forged", {}).allowed_claim()


@pytest.mark.parametrize("name,arguments", [
    ("bash", "pwd"),
    ("bash", []),
    ("read", {"file_path": "x"}),
    ("read", {"path": "x", "file_path": "x"}),
    ("read", {"path": "x", "limit": True}),
    ("bash", {}),
    ("bash", {"command": True}),
    ("bash", {"command": "pwd", "timeout": "1"}),
    ("edit", {"path": "x", "oldText": "old", "newText": "new"}),
    ("edit", {"path": "x", "edits": []}),
    ("edit", {"path": "x", "edits": [{"oldText": False, "newText": "new"}]}),
    ("edit", {"path": "x", "edits": [{"oldText": "old", "newText": "new", "newText_should_not_exist": "extra"}]}),
    ("write", {"path": "x", "content": 3}),
])
def test_pi_argument_records_reject_bad_shapes_without_claiming(name, arguments):
    from agent_comms.native_tool_call import SelectedToolDenied

    from agent_comms.native_tool_call import RefusedCodingCall

    call = CodingCall.decode("bad-argument-shape", name, arguments)
    assert isinstance(call, RefusedCodingCall)
    with pytest.raises(SelectedToolDenied):
        call.allowed_claim()


def test_family_selector_is_denied_but_record_presentation_stays_readable():
    from agent_comms.native_tool_call import SelectedToolDenied
    from agent_comms.tool_declaration import ToolDeclaration

    arguments = {"path": "x", "tool": "write"}
    with pytest.raises(SelectedToolDenied, match="family selector"):
        CodingCall.decode("selector", "read", arguments)
    assert ToolDeclaration.record("read", arguments, call_id="refused") is None


@pytest.mark.asyncio
async def test_malformed_edit_reports_error_and_allows_a_later_tool_round(tmp_path, tool_listener):
    from agent_comms.native_tool_call import SelectedToolDenied
    from agent_comms.selected_tool_broker import consume_selected_slot

    class Owner:
        input_id = secrets.token_hex(16)
        calls = []

        async def allow(self, turn, round_number, call):
            self.calls.append(call.call_id)
            slot = call.slot(self.input_id, round_number)
            consume_selected_slot(tmp_path, slot, slot)
            return NoRoomNeeded()

    owner = Owner()
    socket = CodingToolSocket(tool_listener(tmp_path, secrets.token_hex(32)), owner, pid=os.getpid())
    socket.turn = object()
    malformed = {"path": "x", "edits": [{"oldText": "old", "newText": "new", "newText_should_not_exist": "extra"}]}
    for id, name, args in (("bad-edit", "edit", malformed), ("later-read", "read", {"path": "x"})):
        socket.announce((ToolCallContent(id=id, name=name, arguments=args),))
        socket.tool_started(ToolExecutionStart(tool_call_id=id, tool_name=name, args=args))
        if id == "bad-edit":
            with pytest.raises(SelectedToolDenied, match="newText_should_not_exist") as refused:
                await socket.handle_request(_request(socket, id, name, args))
            response = socket.failure_response(refused.value)
            assert response["ok"] is False and "newText_should_not_exist" in response["error"]
        else:
            assert await socket.handle_request(_request(socket, id, name, args)) == {"ok": True}
        socket.tool_finished(ToolExecutionEnd(tool_call_id=id, tool_name=name, is_error=id == "bad-edit"), owner.input_id)
        socket.assert_complete()
    assert owner.calls == ["later-read"]


@pytest.mark.parametrize("name,arguments,title", [
    ("read", {"path": "x", "offset": 2, "limit": 3}, "Read x"),
    ("bash", {"command": "pwd", "timeout": 2}, "Run pwd"),
    ("edit", {"path": "x", "edits": [{"oldText": "old", "newText": "new"}]}, "Edit x"),
    ("write", {"path": "x", "content": "value"}, "Write x"),
])
def test_pi_argument_fields_own_roundtrip_and_display(name, arguments, title):
    from dataclasses import fields
    from agent_comms.agent_events import ToolStart

    call = CodingCall.decode("typed-argument-shape", name, arguments)
    assert "arguments" not in {item.name for item in fields(call)}
    assert call.arguments == arguments
    assert ToolStart.from_call("typed", name, arguments).title == title


@pytest.mark.asyncio
async def test_multicall_native_transport_matches_events_and_refuses_duplicate(tmp_path, tool_listener):
    class Owner:
        input_id = secrets.token_hex(16)
        calls = []

        async def allow(self, turn, round_number, call):
            self.calls.append(call)
            return NoRoomNeeded()

    owner = Owner()
    tmp_path.chmod(0o700)
    socket = CodingToolSocket(tool_listener(tmp_path, secrets.token_hex(32)), owner, pid=os.getpid())
    socket.turn = object()
    # Actual owner claim_scope/storage is covered separately. This exercises the
    # strict native-event/socket correlation and multiple normal tool calls.
    calls = [
        ("call_one|provider-part", "read", {"path": "a"}),
        ("two", "write", {"path": "b", "content": "c"}),
    ]
    socket.announce([ToolCallContent(id=id, name=name, arguments=args) for id, name, args in calls])
    for id, name, args in calls:
        socket.tool_started(ToolExecutionStart(tool_call_id=id, tool_name=name, args=args))
        raw = (
            json.dumps({"token": socket.listener.token, "call_id": id, "name": name, "arguments": args})
            + "\n"
        ).encode()
        assert await socket.handle_request(raw) == {"ok": True}
        with pytest.raises(ValueError, match="already consumed"):
            await socket.handle_request(raw)
        # Terminal ledger needs the consumed slot an actual owner writes.
        from agent_comms.selected_tool_broker import consume_selected_slot

        call = socket.calls.require(0, id)
        slot = call.slot(owner.input_id, 0)
        consume_selected_slot(tmp_path, slot, slot)
        socket.tool_finished(
            ToolExecutionEnd(tool_call_id=id, tool_name=name, is_error=False), owner.input_id
        )
    socket.assert_complete()
    assert len(owner.calls) == 2


def _request(socket, id, name, args):
    return (
        json.dumps({"token": socket.listener.token, "call_id": id, "name": name, "arguments": args}) + "\n"
    ).encode()


@pytest.mark.asyncio
async def test_tool_error_while_check_waits_cannot_receive_late_permission(tmp_path, tool_listener):
    import asyncio

    checking, release = asyncio.Event(), asyncio.Event()

    class Owner:
        input_id = secrets.token_hex(16)

        async def allow(self, turn, round_number, call):
            checking.set()
            await release.wait()
            return NoRoomNeeded()

    owner = Owner()
    socket = CodingToolSocket(tool_listener(tmp_path, secrets.token_hex(32)), owner, pid=os.getpid())
    socket.turn = object()
    socket.announce((ToolCallContent(id="late", name="read", arguments={"path": "a"}),))
    socket.tool_started(ToolExecutionStart(tool_call_id="late", tool_name="read", args={"path": "a"}))
    pending = asyncio.create_task(socket.handle_request(_request(socket, "late", "read", {"path": "a"})))
    await checking.wait()
    socket.tool_finished(ToolExecutionEnd(tool_call_id="late", tool_name="read", is_error=True), owner.input_id)
    release.set()
    with pytest.raises(ValueError, match="not executing"):
        await pending
    socket.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_child_that_stopped_waiting_is_logged_not_raised(tmp_path, caplog, refused, tool_listener):
    """The live owner logged two tracebacks when Pi gave up on a slow tool check."""
    import asyncio
    import os

    answered = asyncio.Event()

    class Owner:
        input_id = secrets.token_hex(16)

        async def allow(self, turn, round_number, call):
            from agent_comms.native_tool_call import SelectedToolDenied

            answered.set()
            if refused:
                raise SelectedToolDenied("Original owner refusal")
            return NoRoomNeeded()

    tmp_path.chmod(0o700)
    socket = CodingToolSocket(tool_listener(tmp_path, secrets.token_hex(32)), Owner(), pid=os.getpid())
    socket.turn = object()
    await socket.start()
    try:
        call = ("slow", "read", {"path": "a"})
        socket.announce([ToolCallContent(id=call[0], name=call[1], arguments=call[2])])
        reader, writer = await asyncio.open_unix_connection(str(socket.listener.address))
        writer.write(_request(socket, *call))
        await writer.drain()
        # Pi's side: stop waiting and reset the connection before the owner
        # has seen the tool start, which is what its answer waits for.
        writer.transport.abort()
        await asyncio.sleep(0.1)
        socket.tool_started(ToolExecutionStart(tool_call_id=call[0], tool_name=call[1], args=call[2]))
        async with asyncio.timeout(5):
            await answered.wait()
            while socket._handlers:
                await asyncio.sleep(0.01)
    finally:
        await socket.close()
    assert "Unhandled exception" not in caplog.text
    answer = "refused" if refused else "allowed"
    warnings = [record for record in caplog.records if record.name == "agent_comms.native_tool_transport"]
    assert len(warnings) == 1
    assert f"stopped waiting for a tool check before its answer ({answer})" in caplog.text


@pytest.mark.asyncio
async def test_tool_call_pi_never_started_is_cancelled_by_transport_retirement(tmp_path, tool_listener):
    import asyncio
    import os

    class Owner:
        input_id = secrets.token_hex(16)

        async def allow(self, turn, round_number, call):
            raise AssertionError("an unstarted call was checked")

    tmp_path.chmod(0o700)
    socket = CodingToolSocket(tool_listener(tmp_path, secrets.token_hex(32)), Owner(), pid=os.getpid())
    socket.turn = object()
    call = ("never", "read", {"path": "a"})
    socket.announce([ToolCallContent(id=call[0], name=call[1], arguments=call[2])])
    await socket.start()
    reader, writer = await asyncio.open_unix_connection(str(socket.listener.address))
    try:
        writer.write(_request(socket, *call))
        await writer.drain()
        async with asyncio.timeout(5):
            while not socket._handlers or not socket.calls.require(0, call[0]).progress.started._waiters:
                await asyncio.sleep(0)
        await socket.close()
        assert await reader.read() == b""
        assert not socket._handlers
        # Retirement consumes the interrupted request; a late start is not
        # permission to replay it. Main deliberately has no ten-second timer.
        with pytest.raises(ValueError, match="not executing"):
            await socket.handle_request(_request(socket, *call))
    finally:
        writer.close()
        await writer.wait_closed()
        await socket.close()
