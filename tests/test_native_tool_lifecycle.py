"""Both native socket policies share per-call state, not claim_scope authority."""

import asyncio
import json
import os
import secrets
import socket as sockets
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest

import agent_comms.selected_tool_broker as broker
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.storage_room import NoRoomNeeded
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.native_tool_transport import ToolTransport, UnboundToolTransport
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart
from agent_comms.pi_payloads import ToolCallContent


@pytest.fixture(params=["coding", "selected"])
def policy(request, tmp_path, tool_listener):
    tmp_path.chmod(0o700)
    token, input_id = secrets.token_hex(32), secrets.token_hex(16)
    effects = []
    selected = request.param == "selected"
    name = "selected_claimed_write" if selected else "write"
    args = {"resource": "x", "contents": "value"} if selected else {"path": "x", "content": "value"}

    def commit(call, round_number):
        effects.append(call.call_id)
        slot = input_id if selected else call.slot(input_id, round_number)
        ledger_call = call.call_id if selected else slot
        broker.consume_selected_slot(tmp_path, slot, ledger_call)
        if selected:
            broker.record_selected_terminal(tmp_path, slot, ledger_call)

    async def allow_coding(turn, round_number, call):
        commit(call, round_number)
        return NoRoomNeeded()

    socket = (
        broker.SelectedToolSocket(tool_listener(tmp_path, token), lambda call: commit(call, 0), pid=os.getpid())
        if selected
        else CodingToolSocket(
            tool_listener(tmp_path, token),
            SimpleNamespace(allow=allow_coding), pid=os.getpid(),
        )
    )

    socket.turn = SimpleNamespace(command=SimpleNamespace(input_id=input_id))

    def raw(call_id="one", arguments=None):
        values = args if arguments is None else arguments
        body = (
            {"token": token, "request": {"call_id": call_id, "arguments": values}}
            if selected
            else {"token": token, "call_id": call_id, "name": name, "arguments": values}
        )
        return (json.dumps(body) + "\n").encode()

    def announce(call_id="one"):
        socket.announce((ToolCallContent(id=call_id, name=name, arguments=args),))

    def start(call_id="one", **overrides):
        socket.tool_started(
            ToolExecutionStart(
                **{"tool_call_id": call_id, "tool_name": name, "args": args, **overrides}
            )
        )

    def end(call_id="one", **overrides):
        socket.tool_finished(
            ToolExecutionEnd(
                **{"tool_call_id": call_id, "tool_name": name, "is_error": False, **overrides}
            ),
            input_id,
        )

    return SimpleNamespace(
        socket=socket,
        selected=selected,
        effects=effects,
        raw=raw,
        announce=announce,
        start=start,
        end=end,
        root=tmp_path,
        input_id=input_id,
        args=args,
    )


async def test_socket_arrival_before_native_events_waits_without_authority(policy):
    p = policy
    request = asyncio.create_task(p.socket.handle_request(p.raw()))
    await asyncio.sleep(0)
    assert not request.done() and not p.effects
    p.announce()
    await asyncio.sleep(0)
    assert not request.done() and not p.effects
    with pytest.raises(SelectedToolDenied, match="UNKNOWN"):
        p.socket.assert_complete()
    p.start()
    assert await request == {"ok": True}
    with pytest.raises(SelectedToolDenied, match="UNKNOWN"):
        p.socket.assert_complete()  # Claim/durable .done alone is insufficient.
    p.end()
    p.socket.assert_complete()
    assert p.effects == ["one"]
    # A provider id may be reused in a later round. A socket request alone
    # cannot declare that round or reopen the completed call's permission.
    pending = asyncio.create_task(p.socket.handle_request(p.raw()))
    await asyncio.sleep(0)
    assert not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with pytest.raises(SelectedToolDenied):
        p.end()
    assert p.effects == ["one"]


@pytest.mark.parametrize("phase", ["announcement", "start", "terminal"])
async def test_repeated_native_events_never_repeat_effects(policy, phase):
    p = policy
    p.announce()
    if phase == "announcement":
        repeat = p.announce
    else:
        p.start()
        if phase == "start":
            repeat = p.start
        else:
            await p.socket.handle_request(p.raw())
            p.end()
            repeat = p.end
    with pytest.raises(SelectedToolDenied):
        repeat()
    assert p.effects == (["one"] if phase == "terminal" else [])


@pytest.mark.parametrize(
    "observation",
    [
        "start-name",
        "start-id",
        "start-args",
        "terminal-name",
        "terminal-id",
        "terminal-null",
        "no-start",
        "no-claim_scope",
        "socket-args",
    ],
)
async def test_mismatched_observation_does_not_complete(policy, observation):
    p = policy
    p.announce()
    with pytest.raises(SelectedToolDenied):
        if observation == "start-name":
            p.start(tool_name="other")
        elif observation == "start-id":
            p.start("unknown")
        elif observation == "start-args":
            p.start(args={**p.args, "forged": True})
        elif observation == "no-start":
            p.end()
        else:
            p.start()
            if observation == "no-claim_scope":
                p.end()
            elif observation == "socket-args":
                changed = {key: "different" for key in p.args}
                await p.socket.handle_request(p.raw(arguments=changed))
            else:
                await p.socket.handle_request(p.raw())
                if observation == "terminal-name":
                    p.end(tool_name="other")
                elif observation == "terminal-id":
                    p.end("unknown")
                else:
                    p.end(is_error=None)
    with pytest.raises(SelectedToolDenied):
        p.socket.assert_complete()


async def test_cancelled_wait_cannot_be_restarted_or_replayed(policy):
    p = policy
    request = asyncio.create_task(p.socket.handle_request(p.raw()))
    await asyncio.sleep(0)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    with pytest.raises(SelectedToolDenied):
        p.announce()
    with pytest.raises(SelectedToolDenied):
        await p.socket.handle_request(p.raw())
    with pytest.raises(SelectedToolDenied):
        p.socket.assert_complete()
    assert not p.effects


async def test_start_event_not_elapsed_time_owns_authorization(policy):
    p = policy
    request = asyncio.create_task(p.socket.handle_request(p.raw()))
    try:
        # Beyond the removed ten-second limit: load cannot turn a queued start
        # into a refusal, and waiting grants no permission on its own.
        await asyncio.sleep(10.1)
        assert not request.done() and not p.effects
        p.announce()
        p.start()
        assert await request == {"ok": True}
        p.end()
        p.socket.assert_complete()
        assert p.effects == ["one"]
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_delayed_declaration_binds_only_its_exact_request(policy):
    p = policy
    if p.selected:
        return  # Selected policy has one slot, not consecutive normal rounds.
    first = asyncio.create_task(p.socket.handle_request(p.raw("first")))
    second = asyncio.create_task(p.socket.handle_request(p.raw("second")))
    try:
        await asyncio.sleep(0)
        p.announce("first")
        assert p.socket.calls.require(0, "first").call_id == "first"
        with pytest.raises(KeyError):
            p.socket.calls.require(0, "second")
        p.start("first")
        assert await first == {"ok": True}
        p.end("first")
        with pytest.raises(SelectedToolDenied, match="UNKNOWN"):
            p.socket.assert_complete()
        assert p.effects == ["first"] and not second.done()
        p.announce("second")
        p.start("second")
        assert await second == {"ok": True}
        p.end("second")
        p.socket.assert_complete()
        assert p.effects == ["first", "second"]
        assert [round.number for round in p.socket.calls.rounds] == [0, 1]
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_concurrent_duplicate_requests_admit_exactly_once(policy):
    p = policy
    p.announce()
    requests = [asyncio.create_task(p.socket.handle_request(p.raw())) for _ in range(2)]
    await asyncio.sleep(0)
    p.start()
    results = await asyncio.gather(*requests, return_exceptions=True)
    assert sum(result == {"ok": True} for result in results) == 1
    assert sum(isinstance(result, SelectedToolDenied) for result in results) == 1
    assert p.effects == ["one"]
    p.end()
    p.socket.assert_complete()


async def test_socket_close_cancels_pending_claim_and_closes_client(policy):
    p = policy
    await p.socket.start()
    reader, writer = await asyncio.open_unix_connection(str(p.socket.listener.path))
    writer.write(p.raw())
    await writer.drain()
    async with asyncio.timeout(2):
        while not p.socket.calls:
            await asyncio.sleep(0)
    await p.socket.close()
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    assert not p.socket.listener.path.exists() and not p.socket._handlers
    with pytest.raises(SelectedToolDenied):
        p.socket.assert_complete()
    assert not p.effects


async def test_interleaved_calls_wait_on_their_own_native_start(tmp_path, tool_listener):
    tmp_path.chmod(0o700)
    observed = []

    async def allow_coding(turn, round_number, call):
        observed.append(call.call_id)
        return NoRoomNeeded()

    socket = CodingToolSocket(
        tool_listener(tmp_path, secrets.token_hex(32)),
        SimpleNamespace(allow=allow_coding), pid=os.getpid(),
    )
    socket.turn = SimpleNamespace(command=SimpleNamespace(input_id=secrets.token_hex(16)))
    arguments = {"path": "x"}
    socket.announce(
        tuple(ToolCallContent(id=id, name="read", arguments=arguments) for id in ("a", "b"))
    )

    def raw(id):
        return (
            json.dumps(
                {"token": socket.listener.token, "call_id": id, "name": "read", "arguments": arguments}
            )
            + "\n"
        ).encode()

    requests = {id: asyncio.create_task(socket.handle_request(raw(id))) for id in ("a", "b")}
    await asyncio.sleep(0)
    socket.tool_started(ToolExecutionStart(tool_call_id="b", tool_name="read", args=arguments))
    assert await requests["b"] == {"ok": True}
    assert not requests["a"].done() and observed == ["b"]
    socket.tool_started(ToolExecutionStart(tool_call_id="a", tool_name="read", args=arguments))
    assert await requests["a"] == {"ok": True}
    assert observed == ["b", "a"]


def test_empty_tool_round_cannot_supply_completion(policy):
    with pytest.raises(SelectedToolDenied):
        policy.socket.announce(())


def test_selected_slot_rejects_second_call(policy):
    if not policy.selected:
        return
    policy.announce()
    with pytest.raises(SelectedToolDenied):
        policy.announce("second")


@pytest.mark.parametrize("bind_child", [False, True])
async def test_only_child_bound_transport_can_reply(tmp_path, bind_child):
    turn = SimpleNamespace(launch=SimpleNamespace(session=SimpleNamespace(directory=tmp_path)))
    async with AsyncExitStack() as resources:
        unbound = await ToolTransport.open(turn, resources)
        assert isinstance(unbound, UnboundToolTransport)
        assert not hasattr(unbound, "_reply") and not hasattr(unbound, "_handle")
        with pytest.raises(TypeError, match="pid"):
            ToolTransport(unbound.listener)
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_unix_connection(str(unbound.listener.address))
        if bind_child:
            bound = await unbound.bind_process(os.getpid())
            assert isinstance(bound, ToolTransport)
            assert bound.expected_pid == os.getpid()
            assert bound.listener.server.is_serving()
    assert not (tmp_path / "s").exists()


async def test_malformed_child_json_receives_a_declared_refusal(policy):
    p = policy
    await p.socket.start()
    reader, writer = await asyncio.open_unix_connection(str(p.socket.listener.address))
    try:
        writer.write(b"{\n")
        await writer.drain()
        assert json.loads(await reader.readline())["ok"] is False
        assert await reader.read() == b""
        assert not p.effects
    finally:
        writer.close()
        await writer.wait_closed()
