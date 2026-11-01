"""Provider-free selected-tool contract tests; no native Pi/provider launch."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import closing
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_comms.selected_tool_broker as broker
from agent_comms.native_tool_call import SelectedToolBusy
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import WakeClaim
from agent_comms.nominal.field_codec import (
    AssignmentId,
    ClaimId,
    FieldCodec,
    InputId,
    MessageId,
    ToolCallId,
    TurnId,
)
from agent_comms.native_runtime_input import AcknowledgedSlot, ConsumedSlot, SelectedToolSlot
from agent_comms.native_tool_call import (
    CodingCall,
    DuplicateToolCallIdentity,
    OwnerToolConnection,
)
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart
from agent_comms.pi_payloads import ToolCallContent
from agent_comms.repo import Checkout
from agent_comms.storage_room import NoRoomNeeded
from agent_comms.threads import Thread
from agent_comms.tool_denial import SelectedToolDenied


@pytest.fixture
def slot_store(private_comms):
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        yield store


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
    tmp_path: Path, tool_listener, undealt_gate
) -> None:
    class Owner:
        input_id = secrets.token_hex(16)

        async def allow(self, round_number, call) -> None:
            raise AssertionError("invalid round must not allow a call")

    socket = CodingToolSocket(
        tool_listener(tmp_path, secrets.token_hex(32)), Owner(), undealt_gate, pid=os.getpid()
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
        broker.SelectedToolMode(None, None)  # type: ignore[arg-type]


def test_a_busy_write_refuses_the_call_as_busy_and_consumes_nothing(slot_store) -> None:
    """Real contention through the real session: busy, never the UNKNOWN of a started write."""
    call = FieldCodec.decode(ToolCallId, "call_1")
    slot_id = broker.SelectedToolRequest(
        call, broker.SelectedWriteArguments("notes.txt", "write"),
    ).slot(InputId.mint(), 0)
    with closing(sqlite3.connect(slot_store.session.path, isolation_level=None)) as held:
        held.execute("BEGIN EXCLUSIVE")
        with pytest.raises(SelectedToolBusy):
            broker.consume_selected_slot(slot_store, slot_id, call)
        held.execute("ROLLBACK")
    broker.consume_selected_slot(slot_store, slot_id, call)
    assert SelectedToolSlot.require(slot_store.session._connection, slot_id=slot_id).call_id == "call_1"


def test_consumption_is_once_only_and_terminal_is_separate(slot_store) -> None:
    call_1, call_2 = (FieldCodec.decode(ToolCallId, text) for text in ("call_1", "call_2"))
    request = broker.SelectedToolRequest(
        call_1, broker.SelectedWriteArguments("notes.txt", "write"),
    )
    slot_id = request.slot(InputId.mint(), 0)
    broker.consume_selected_slot(slot_store, slot_id, call_1)
    row = SelectedToolSlot.require(slot_store.session._connection, slot_id=slot_id)
    assert row.call_id == "call_1"
    assert row.state is ConsumedSlot
    with pytest.raises(SelectedToolDenied):
        broker.verify_selected_terminal(slot_store.session.path, slot_id, call_1)
    with pytest.raises(SelectedToolDenied):
        broker.consume_selected_slot(slot_store, slot_id, call_2)
    broker.record_selected_terminal(slot_store.session.path, slot_id, call_1)
    broker.verify_selected_terminal(slot_store.session.path, slot_id, call_1)
    assert (
        SelectedToolSlot.require(slot_store.session._connection, slot_id=slot_id).state
        is AcknowledgedSlot
    )
    with pytest.raises(SelectedToolDenied):
        broker.record_selected_terminal(slot_store.session.path, slot_id, call_1)


@pytest.mark.asyncio
async def test_prebegin_busy_keeps_native_socket_for_the_next_fresh_call(
    private_comms, tmp_path: Path, tool_listener, undealt_gate,
) -> None:
    path = CoordinationStore.path_in(private_comms.root)
    input_id = InputId.mint()

    class SlotOwner:
        async def allow(self, turn, round_number, call):
            await Coordination.run_async(
                path,
                lambda store: broker.consume_selected_slot(
                    store, call.slot(input_id, round_number), call.call_id,
                ),
            )
            return NoRoomNeeded()

    async with Coordination.worker(path):
        await Coordination.run_async(path, lambda store: None)
        listener = tool_listener(tmp_path, secrets.token_hex(32))
        server = CodingToolSocket(listener, SlotOwner(), undealt_gate, pid=os.getpid())
        server.turn = object()
        arguments = {"path": str(tmp_path / "read-only-proof")}
        writer = sqlite3.connect(path, isolation_level=None)
        await server.start()
        try:
            writer.execute("BEGIN EXCLUSIVE")
            for round_number, call_id in enumerate(("busy", "after-busy")):
                call = CodingCall.decode(call_id, "read", arguments)
                server.announce((ToolCallContent(id=call_id, name="read", arguments=arguments),))
                server.tool_started(ToolExecutionStart(
                    tool_call_id=call_id, tool_name="read", args=arguments,
                ))
                payload = (json.dumps({
                    "token": listener.token, "call_id": call_id,
                    "name": "read", "arguments": arguments,
                }) + "\n").encode()
                assert await query(listener.connection, payload) is bool(round_number)
                server.calls.require(round_number, call_id).finish(
                    "read", not bool(round_number), path, input_id, round_number,
                )
                assert listener.server.is_serving()
                if round_number == 0:
                    writer.execute("ROLLBACK")
                    with Coordination(str(path)) as store:
                        assert SelectedToolSlot.one(
                            store.session._connection, slot_id=call.slot(input_id, round_number),
                        ) is None
            with Coordination(str(path)) as store:
                SelectedToolSlot.require(
                    store.session._connection, slot_id=call.slot(input_id, round_number),
                ).verify(call.call_id)
        finally:
            writer.close()
            await server.close()


def test_openai_responses_call_id_is_consumed_as_the_provider_spells_it(slot_store) -> None:
    provider_id = (
        "call_082e4aa2372a4cb2872b5eaa6920b3d1|fc_061e57fb77d34295016ad6e0376e78819eb0dc71a4eb1a6725"
    )
    call = CodingCall.decode(provider_id, "read", {"path": "README.md"})
    slot_id = call.slot(InputId.mint(), 0)
    broker.consume_selected_slot(slot_store, slot_id, call.call_id)
    row = SelectedToolSlot.require(slot_store.session._connection, slot_id=slot_id)
    assert row.call_id == provider_id
    for malformed in ("", "call one", "x" * 257):
        with pytest.raises(SelectedToolDenied):
            CodingCall.decode(malformed, "read", {"path": "README.md"})


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_owner_mode_denies_unreserved_input_before_consuming_ledger(
        private_comms, tmp_path: Path) -> None:
    comms = private_comms
    comms.registry.declare(Thread("owner", frozenset(), Checkout(tmp_path)))
    claim_scope = WakeClaim(
        wire_root_id="a" * 32,
        source_seq=1,
        source_message_id=MessageId.mint("source"),
        wake_assignment_id=AssignmentId.cohort(b"owner-mode-wake"),
        wake_revision=1,
        recipient_lookup="c" * 32,
        execution_id="execution",
        operation_id=ClaimId("d" * 32),
        owner_claimed_generation=1,
        turn_id=TurnId('9b473845f684444f80a26586ae298d2b'),
        participant_generation=1,
        attempt_ordinal=1,
    )
    with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
        input_id = InputId.mint()
        mode = broker.selected_tool_mode_for_owner(
            comms, store, claim_scope, "owner", input_id
        )
        with pytest.raises(SelectedToolDenied):
            mode.action(
                0, broker.SelectedToolRequest(
                    "call_1", broker.SelectedWriteArguments("notes.txt", "write")
                )
            )
        assert SelectedToolSlot.count(store.session._connection) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool socket requires SO_PEERCRED")
def test_fake_socket_requires_pid_token_matching_emitted_call(
    tmp_path: Path, tool_listener, undealt_gate
) -> None:
    tmp_path.chmod(0o700)
    token = secrets.token_hex(32)
    observed: list[broker.SelectedToolRequest] = []

    async def run() -> None:
        wrong = broker.SelectedToolSocket(
            tool_listener(tmp_path, token),
            lambda round_number, call: observed.append(call), undealt_gate,
            pid=os.getpid() + 100000
        )
        await wrong.start()
        try:
            assert not await query(wrong.listener.connection, _wire(token))
        finally:
            await wrong.close()
        server = broker.SelectedToolSocket(
            tool_listener(tmp_path, token),
            lambda round_number, call: observed.append(call), undealt_gate, pid=os.getpid()
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
async def test_listener_isolated_from_previous_turn_and_keeps_consumed_slot(
    tmp_path: Path,
    tool_listener,
    abandoned: bool,
    slot_store,
    undealt_gate,
) -> None:
    tmp_path.chmod(0o700)
    previous = tool_listener(tmp_path, secrets.token_hex(32))
    if abandoned:
        # A dead process closes its FD but leaves the socket inode and directory.
        previous.socket.close()
    previous_inode = previous.path.stat().st_ino
    token = secrets.token_hex(32)
    input_id = InputId.mint()
    observed: list[broker.SelectedToolRequest] = []

    def accept(round_number: int, request: broker.SelectedToolRequest) -> None:
        slot_id = request.slot(input_id, round_number)
        with Coordination(slot_store.session.path) as store:
            broker.consume_selected_slot(store, slot_id, request.call_id)
            broker.record_selected_terminal(store.session.path, slot_id, request.call_id)
        observed.append(request)

    server = broker.SelectedToolSocket(
        tool_listener(tmp_path, token), accept, undealt_gate, pid=os.getpid())
    server.turn = SimpleNamespace(launch=SimpleNamespace(root=Path(slot_store.session.path).parent))
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
        assert (SelectedToolSlot.require(
            slot_store.session._connection, slot_id=observed[0].slot(input_id, 0),
        ).state is AcknowledgedSlot)
    finally:
        await server.close()
    assert not server.listener.path.exists()
    assert not server.listener.path.parent.exists()
    assert previous.path.stat().st_ino == previous_inode


@pytest.mark.asyncio
async def test_a_dealt_rule_gate_refuses_the_selected_write_before_its_owner_action(
    tmp_path: Path, tool_listener
) -> None:
    """The selected write rides the shared gated transport: an unread deal refuses it before
    the owner callback could claim or write anything."""
    from agent_comms.pi_rule_gate import PiRuleGate, RulesUnread
    from agent_comms.rule_deal import StartedDeal
    from agent_comms_hooks.skill import DeckReader

    gate = PiRuleGate(tmp_path / "scratch", tmp_path)
    gate.deal(DeckReader, (), StartedDeal())
    token = secrets.token_hex(32)
    acted: list[broker.SelectedToolRequest] = []
    server = broker.SelectedToolSocket(
        tool_listener(tmp_path, token), lambda round_number, call: acted.append(call), gate,
        pid=os.getpid())
    arguments = {"resource": "notes.txt", "contents": "é"}
    name = broker.SelectedToolRequest.declared_name
    server.announce((ToolCallContent(id="call_1", name=name, arguments=arguments),))
    server.tool_started(ToolExecutionStart(tool_call_id="call_1", tool_name=name, args=arguments))
    with pytest.raises(RulesUnread, match="before reading"):
        await server.handle_request(_wire(token))
    assert acted == []
