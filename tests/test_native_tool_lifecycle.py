"""Both native socket policies share per-call state, not admission authority."""

import asyncio
import json
import os
import secrets
from types import SimpleNamespace

import pytest

from agent_comms import selected_tool_broker as broker
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart
from agent_comms.pi_payloads import ToolCallContent


@pytest.fixture(params=["coding", "selected"])
def policy(request, tmp_path):
    tmp_path.chmod(0o700)
    token, input_id = secrets.token_hex(32), secrets.token_hex(16)
    effects = []
    selected = request.param == "selected"
    name = "selected_claimed_write" if selected else "write"
    args = {"resource": "x", "contents": "value"} if selected else {"path": "x", "content": "value"}

    def action(call):
        effects.append(call.call_id)
        slot = input_id if selected else call.slot(input_id)
        ledger_call = call.call_id if selected else slot
        broker.consume_selected_slot(tmp_path, slot, ledger_call)
        if selected:
            broker.record_selected_terminal(tmp_path, slot, ledger_call)

    socket = (
        broker.SelectedToolSocket(tmp_path, token, action)
        if selected
        else CodingToolSocket(tmp_path, token, SimpleNamespace(admit=action))
    )

    def raw(call_id="one", arguments=None):
        values = args if arguments is None else arguments
        body = {"token": token, "call_id": call_id}
        body.update(values if selected else {"name": name, "arguments": values})
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
        p.socket.assert_complete()  # Admission/durable .done alone is insufficient.
    p.end()
    p.socket.assert_complete()
    assert p.effects == ["one"]
    with pytest.raises(SelectedToolDenied):
        await p.socket.handle_request(p.raw())
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
        "no-admission",
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
            if observation == "no-admission":
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


@pytest.mark.parametrize("failure", ["owner-revoked", "slot-fsync", "cancel-action"])
async def test_failed_admission_is_consumed_and_never_retried(policy, monkeypatch, failure):
    p = policy
    p.announce()
    p.start()
    if failure == "slot-fsync":
        monkeypatch.setattr(
            broker.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("lost fsync"))
        )
    else:

        def fail(_call):
            p.effects.append("attempted")
            if failure == "cancel-action":
                raise asyncio.CancelledError()
            raise SelectedToolDenied("owner revoked")

        if p.selected:
            p.socket.action = fail
        else:
            p.socket.owner.admit = fail
    with pytest.raises((SelectedToolDenied, asyncio.CancelledError)):
        await p.socket.handle_request(p.raw())
    count = len(p.effects)
    with pytest.raises(SelectedToolDenied):
        await p.socket.handle_request(p.raw())
    assert len(p.effects) == count
    if p.selected or failure == "cancel-action":
        with pytest.raises(SelectedToolDenied):
            p.end(is_error=True)
        with pytest.raises(SelectedToolDenied):
            p.socket.assert_complete()
    else:
        p.end(is_error=True)  # Normal Pi may report denied tools; no successful mutation claimed.
        p.socket.assert_complete()


async def test_lost_terminal_receipt_cannot_be_replaced_by_error(policy, monkeypatch):
    p = policy
    p.announce()
    p.start()
    await p.socket.handle_request(p.raw())
    if p.selected:
        (p.root / "selected-tool-ledger" / (p.input_id + ".done")).unlink()
    else:
        monkeypatch.setattr(
            broker.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("terminal fsync"))
        )
    with pytest.raises((SelectedToolDenied, OSError)):
        p.end()
    with pytest.raises(SelectedToolDenied):
        p.end(is_error=True)
    with pytest.raises(SelectedToolDenied):
        p.socket.assert_complete()
    assert p.effects == ["one"]


async def test_socket_close_cancels_pending_admission_and_closes_client(policy):
    p = policy
    await p.socket.start()
    p.socket.expected_pid = os.getpid()
    reader, writer = await asyncio.open_unix_connection(str(p.socket.path))
    writer.write(p.raw())
    await writer.drain()
    async with asyncio.timeout(2):
        while not p.socket.calls:
            await asyncio.sleep(0)
    await p.socket.close()
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    assert not p.socket.path.exists() and not p.socket._handlers
    with pytest.raises(SelectedToolDenied):
        p.socket.assert_complete()
    assert not p.effects


async def test_interleaved_calls_wait_on_their_own_native_start(tmp_path):
    tmp_path.chmod(0o700)
    observed = []
    socket = CodingToolSocket(
        tmp_path, secrets.token_hex(32), SimpleNamespace(admit=lambda c: observed.append(c.call_id))
    )
    arguments = {"path": "x"}
    socket.announce(
        tuple(ToolCallContent(id=id, name="read", arguments=arguments) for id in ("a", "b"))
    )

    def raw(id):
        return (
            json.dumps(
                {"token": socket.token, "call_id": id, "name": "read", "arguments": arguments}
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
