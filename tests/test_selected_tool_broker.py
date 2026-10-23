"""Provider-free selected-tool contract tests; no native Pi/provider launch."""

from __future__ import annotations

from agent_comms.private_runtime_schema import PrivateCoordinatorTable
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

import pytest

import agent_comms.selected_tool_broker as broker
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import WakeClaim
from agent_comms.native_tool_call import (
    DuplicateToolCallIdentity,
    OwnerToolConnection,
    SelectedToolDenied,
)
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart
from agent_comms.pi_payloads import ToolCallContent


def _wire(token: str, **changes: object) -> bytes:
    arguments = {"resource": "notes.txt", "contents": "é", **changes}
    data = {"token": token, "request": {"call_id": "call_1", "arguments": arguments}}
    return (json.dumps(data) + "\n").encode("utf-8")


async def query(connection: OwnerToolConnection, payload: bytes) -> bool:
    reader, writer = await asyncio.open_unix_connection(connection.path)
    try:
        writer.write(payload)
        await writer.drain()
        return json.loads(await reader.readline())["ok"] is True
    finally:
        writer.close()
        await writer.wait_closed()


def test_duplicate_call_id_in_one_tool_round_is_named_decode_failure(
    tmp_path: Path, tool_listener
) -> None:
    class Owner:
        input_id = secrets.token_hex(16)

        async def allow(self, round_number, call) -> None:
            raise AssertionError("invalid round must not allow a call")

    socket = CodingToolSocket(
        tool_listener(tmp_path, secrets.token_hex(32)), Owner(), pid=os.getpid()
    )
    repeated = ToolCallContent(id="duplicate", name="read", arguments={"path": "one"})
    with pytest.raises(DuplicateToolCallIdentity):
        socket.announce((repeated, repeated))
    assert not socket.calls


def test_pre_turn_intent_is_distinct_from_owner_bound_mode() -> None:
    intent = broker.SelectedToolIntent()
    assert type(intent) is broker.SelectedToolIntent
    assert not isinstance(intent, broker.SelectedToolMode)
    assert not hasattr(intent, "action")
    with pytest.raises(TypeError):
        broker.SelectedToolMode(None)  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_consumption_is_once_only_and_terminal_is_separate(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    input_id = secrets.token_hex(16)
    broker.consume_selected_slot(tmp_path, input_id, "call_1")
    ledger = tmp_path / "selected-tool-ledger"
    assert (ledger / input_id).read_text() == "call_1\n"
    assert not (ledger / (input_id + ".done")).exists()
    with pytest.raises(SelectedToolDenied):
        broker.consume_selected_slot(tmp_path, input_id, "call_2")
    broker.record_selected_terminal(tmp_path, input_id, "call_1")
    with pytest.raises(SelectedToolDenied):
        broker.record_selected_terminal(tmp_path, input_id, "call_1")


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_owner_mode_denies_unreserved_input_before_consuming_ledger(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    comms = Comms(root)
    claim_scope = WakeClaim(
        wire_root_id="a" * 32,
        source_seq=1,
        source_message_id="source",
        wake_assignment_id="cohort-v1:" + "b" * 64,
        wake_revision=1,
        recipient_lookup="c" * 32,
        execution_id="execution",
        operation_id="d" * 32,
        owner_claimed_generation=1,
        turn_id="turn",
        participant_generation=1,
        attempt_ordinal=1,
    )
    with Coordination(str(root / "coordination.sqlite3")) as store:
        PrivateCoordinatorTable.install(store)
        input_id = secrets.token_hex(16)
        mode = broker.selected_tool_mode_for_owner(
            comms, store, claim_scope, "owner", session_dir, input_id
        )
        with pytest.raises(SelectedToolDenied):
            mode.action(
                broker.SelectedToolRequest(
                    "call_1", broker.SelectedWriteArguments("notes.txt", "write")
                )
            )
        assert not (session_dir / "selected-tool-ledger").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool socket requires SO_PEERCRED")
def test_fake_socket_requires_pid_token_matching_emitted_call(
    tmp_path: Path, tool_listener
) -> None:
    tmp_path.chmod(0o700)
    token = secrets.token_hex(32)
    observed: list[broker.SelectedToolRequest] = []

    async def run() -> None:
        wrong = broker.SelectedToolSocket(
            tool_listener(tmp_path, token), observed.append, pid=os.getpid() + 100000
        )
        await wrong.start()
        try:
            assert not await query(wrong.listener.connection, _wire(token))
        finally:
            await wrong.close()
        server = broker.SelectedToolSocket(
            tool_listener(tmp_path, token), observed.append, pid=os.getpid()
        )
        await server.start()
        try:
            for changes in (
                {"resource": "../escape"},
                {"resource": "/absolute"},
                {"resource": "@alias"},
                {"resource": "./alias"},
                {"contents": "x" * (128 * 1024 + 1)},
                {"contents": 12},
                {"contents": "\ud800"},
                {"root": "forged"},
            ):
                assert not await query(server.listener.connection, _wire(token, **changes))
            assert not observed and not server.calls
            arguments = {"resource": "notes.txt", "contents": "é"}
            pending = asyncio.create_task(query(server.listener.connection, _wire(token)))
            await asyncio.sleep(0)
            assert not pending.done()  # Socket arrival is not a native start.
            server.announce(
                [ToolCallContent(id="call_1", name="selected_claimed_write", arguments=arguments)]
            )
            with pytest.raises(SelectedToolDenied):
                server.tool_started(
                    ToolExecutionStart(
                        tool_call_id="call_1",
                        tool_name="selected_claimed_write",
                        args={**arguments, "contents": "different"},
                    )
                )
            server.tool_started(
                ToolExecutionStart(
                    tool_call_id="call_1", tool_name="selected_claimed_write", args=arguments
                )
            )
            assert await pending
            assert not await query(server.listener.connection, _wire("0" * 64))
            assert not await query(
                server.listener.connection, _wire(token)
            )  # Consumed exactly once.
            assert observed == [
                broker.SelectedToolRequest(
                    "call_1", broker.SelectedWriteArguments("notes.txt", "é")
                )
            ]
        finally:
            await server.close()
        assert not server.listener.path.exists()
        assert not server.listener.path.parent.exists()

    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("abandoned", (False, True))
async def test_listener_isolated_from_previous_turn_and_keeps_session_ledger(
    tmp_path: Path,
    tool_listener,
    abandoned: bool,
) -> None:
    tmp_path.chmod(0o700)
    previous = tool_listener(tmp_path, secrets.token_hex(32))
    if abandoned:
        # A dead process closes its FD but leaves the socket inode and directory.
        previous.socket.close()
    previous_inode = previous.path.stat().st_ino
    token = secrets.token_hex(32)
    input_id = secrets.token_hex(16)
    observed: list[broker.SelectedToolRequest] = []

    def accept(request: broker.SelectedToolRequest) -> None:
        broker.consume_selected_slot(tmp_path, input_id, request.call_id)
        broker.record_selected_terminal(tmp_path, input_id, request.call_id)
        observed.append(request)

    server = broker.SelectedToolSocket(tool_listener(tmp_path, token), accept, pid=os.getpid())
    assert server.listener.path != previous.path
    assert server.listener.path.parent.stat().st_mode & 0o777 == 0o700
    await server.start()
    try:
        arguments = {"resource": "notes.txt", "contents": "é"}
        server.announce(
            (
                ToolCallContent(
                    id="call_1",
                    name="selected_claimed_write",
                    arguments=arguments,
                ),
            )
        )
        server.tool_started(
            ToolExecutionStart(
                tool_call_id="call_1",
                tool_name="selected_claimed_write",
                args=arguments,
            )
        )
        assert await query(server.listener.connection, _wire(token))
        server.tool_finished(
            ToolExecutionEnd(
                tool_call_id="call_1",
                tool_name="selected_claimed_write",
                is_error=False,
            ),
            input_id,
        )
        server.assert_complete()
        assert observed == [
            broker.SelectedToolRequest(
                "call_1",
                broker.SelectedWriteArguments("notes.txt", "é"),
            )
        ]
        assert (tmp_path / "selected-tool-ledger" / (input_id + ".done")).exists()
    finally:
        await server.close()
    assert not server.listener.path.exists()
    assert not server.listener.path.parent.exists()
    assert previous.path.stat().st_ino == previous_inode
