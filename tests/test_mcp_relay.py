"""Provider-free Pi extension UI relay controls over a real detached child pipe."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import PromptResponse

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms import pi_events as pi
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    McpClientReceiptUpdate,
    decode_updates,
)
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import McpLiveReceipt
from agent_comms.runtime import UNBOUND_CONTROLLER, RuntimeProxy, SocketClient
from delivery_owner_fixture import canonical_agent

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable stub"),
    pytest.mark.usefixtures("native_rpc_fixture"),
]


async def test_live_projection_requires_owning_acp_turn_and_session(tmp_path):
    owner = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
    updates = []

    class Observer:
        async def session_update(self, **kwargs):
            updates.append(kwargs)

    observer = Observer()
    receipt = McpLiveReceipt(1, "pi-mcp-client", "a" * 32, "running", "turn", ())
    event = ae.McpLiveStatus(receipt)
    try:
        owner.turns.active_turns["session-1"] = "turn-1"
        await owner._emit_event("session-1", event, observer, turn_id="turn-1")
        assert len(updates) == 1
        assert updates[0]["session_id"] == "session-1"
        assert decode_updates(updates[0]["update"].field_meta) == (
            McpClientReceiptUpdate("turn-1", receipt),
        )
        owner.turns.active_turns.pop("session-1")  # Settled: no receipt may escape.
        await owner._emit_event("session-1", event, observer, turn_id="turn-1")
        owner.turns.active_turns["session-1"] = "turn-2"
        # A stale receipt arriving FIRST in a successor turn still loses.
        await owner._emit_event("session-1", event, observer, turn_id="turn-1")
        await owner._emit_event("session-2", event, observer, turn_id="turn-1")
        await owner._emit_event("session-1", event, observer, turn_id=None)
        assert len(updates) == 1
        await owner._emit_event("session-1", event, observer, turn_id="turn-2")
        assert len(updates) == 2
        assert decode_updates(updates[-1]["update"].field_meta)[0].turn_id == "turn-2"
    finally:
        await owner.shutdown()


def stub(tmp_path: Path, body: str) -> str:
    path = tmp_path / "pi-rpc-stub"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize(
    "method,choice,answer",
    [
        ("confirm", pi.ConfirmedUiChoice(True), {"confirmed": True}),
        ("confirm", None, {"cancelled": True}),
        ("confirm", pi.ValueUiChoice("forged"), {"cancelled": True}),
        ("select", pi.ValueUiChoice("one"), {"value": "one"}),
        ("select", pi.ValueUiChoice("forged"), {"cancelled": True}),
    ],
)
async def test_same_child_ui_reply_is_correlated_and_denied_without_controller(
    tmp_path, method, choice, answer
):
    marker = tmp_path / "received.json"
    program = stub(
        tmp_path,
        f"""
import json, sys
send = lambda row: print(json.dumps(row), flush=True)
state = json.loads(sys.stdin.readline())
send({{"id": state["id"], "type": "response", "command": "get_state", "success": True,
      "data": {{"nativeInputProofCapability": {json.dumps(backend.NATIVE_INPUT_CAPABILITY)},
                "sessionId": "fixture-pi-session"}}}})
prompt = json.loads(sys.stdin.readline())
send({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
send({{"type": "message_start", "message": {{"role": "user", "content": prompt["message"],
      "inputId": prompt["inputId"]}}}})
send({
            json.dumps(
                {
                    "type": "extension_ui_request",
                    "id": "owned-ui-1",
                    "method": method,
                    "title": "Run one operation?",
                    "message": "exact request",
                    "options": ["one", "two"],
                }
            )
        })
reply = json.loads(sys.stdin.readline())
with open({str(marker)!r}, "w") as output: json.dump(reply, output)
send({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop"}}}})
send({{"type": "agent_settled"}})
""",
    )
    requests = []

    async def controller(request):
        requests.append(request)
        return choice

    events = await asyncio.wait_for(
        collect_backend(program, tmp_path, controller if choice is not None else None), timeout=6
    )
    assert marker.exists()
    assert json.loads(marker.read_text()) == {
        "type": "extension_ui_response",
        "id": "owned-ui-1",
        **answer,
    }
    assert len(requests) == (0 if choice is None else 1)
    assert isinstance(events[-1], ae.Done)


async def collect_backend(program, cwd, controller):
    return [
        event
        async for event in backend.stream_agent_events(
            program, [], "fixture", str(cwd), ui_request=controller
        )
    ]


async def test_owner_permission_only_for_bound_live_subscriber_and_turn(tmp_path, monkeypatch):
    agent = canonical_agent(wire(tmp_path / "wire"))
    await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    session_id = "project"
    turn = "turn-1"
    agent.turns.active_turns[session_id] = turn
    request = pi.ExtensionUiRequest(
        id="ui-1", method="confirm", title="Confirm", message="One action"
    )

    class DirectController:
        async def request_permission(self, **kwargs):
            assert kwargs["session_id"] == session_id
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

    controller = DirectController()
    agent.sessions.client = controller
    assert (
        await agent.turns.extension_ui_permission(session_id, turn, controller, request)
    ) == pi.ConfirmedUiChoice(True)
    assert (
        await agent.turns.extension_ui_permission(session_id, "wrong-turn", controller, request)
    ) == pi.CancelledUiChoice()
    assert (
        await agent.turns.extension_ui_permission(session_id, turn, None, request)
        == pi.CancelledUiChoice()
    )
    assert agent._runtime.controller.get() is UNBOUND_CONTROLLER
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

    controller.request_permission = delayed
    in_flight = asyncio.create_task(
        agent.turns.extension_ui_permission(session_id, turn, controller, request)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    agent.turns.active_turns[session_id] = "successor-turn"
    release.set()
    assert await asyncio.wait_for(in_flight, timeout=1) == pi.CancelledUiChoice()
    agent.turns.active_turns[session_id] = turn
    monkeypatch.setattr("agent_comms.turn_runner.ACP_PERMISSION_TIMEOUT_SECONDS", 0.05)

    async def unresponsive(**kwargs):
        await asyncio.Event().wait()

    controller.request_permission = unresponsive
    assert (
        await asyncio.wait_for(
            agent.turns.extension_ui_permission(session_id, turn, controller, request), timeout=1
        )
        == pi.CancelledUiChoice()
    )
    await agent.shutdown()


def test_live_receipt_rejects_stale_input_malformed_and_ambiguous_claims():
    input_id = "a" * 32
    valid = {
        "version": 1,
        "source": "pi-mcp-client",
        "inputId": input_id,
        "state": "running",
        "lifetime": "turn",
        "servers": [
            {
                "id": "one",
                "scope": "user",
                "state": "ready",
                "calls": "confirm",
                "tools": 1,
                "resources": 0,
                "prompts": 0,
            }
        ],
    }

    def wire_claim(claim):
        from agent_comms.pi_events import ExtensionUiRequest

        return ExtensionUiRequest(
            method="setStatus", status_key="pi-mcp/live-v1", status_text=claim
        )

    assert backend._pi_mcp_live_receipt(
        wire_claim(json.dumps(valid)), input_id
    ) == FieldCodec.decode(McpLiveReceipt, valid)
    assert backend._pi_mcp_live_receipt(wire_claim(json.dumps(valid)), "b" * 32) is None
    assert (
        backend._pi_mcp_live_receipt(
            wire_claim(json.dumps(valid).replace('"version": 1', '"version": 1, "version": 1')),
            input_id,
        )
        is None
    )
    valid["servers"][0]["calls"] = "automatic"
    valid["servers"][0]["state"] = "denied"
    assert backend._pi_mcp_live_receipt(wire_claim(json.dumps(valid)), input_id) is None
    valid["servers"][0]["calls"] = "unavailable"
    valid["servers"][0]["id"] = "\x1b[2J"
    assert backend._pi_mcp_live_receipt(wire_claim(json.dumps(valid)), input_id) is None


async def test_package_live_receipt_after_settlement_is_not_reprojected(tmp_path):
    input_receipt = {
        "version": 1,
        "source": "pi-mcp-client",
        "state": "running",
        "lifetime": "turn",
        "servers": [],
    }
    program = stub(
        tmp_path,
        f"""
import json, sys
send = lambda row: print(json.dumps(row), flush=True)
preflight = json.loads(sys.stdin.readline())
send({{"id":preflight["id"],"type":"response","command":"get_state","success":True,
      "data":{{"nativeInputProofCapability":{json.dumps(backend.NATIVE_INPUT_CAPABILITY)},
              "sessionId":"fixture-session"}}}})
prompt = json.loads(sys.stdin.readline())
send({{"id":prompt["id"],"type":"response","command":"prompt","success":True}})
send({{"type":"message_start","message":{{"role":"user","content":prompt["message"],
      "inputId":prompt["inputId"]}}}})
send({{"type":"message_end","message":{{"role":"assistant","stopReason":"stop"}}}})
send({{"type":"agent_settled"}})
receipt = {input_receipt!r}
receipt["inputId"] = prompt["inputId"]
send({{"type":"extension_ui_request","id":"late","method":"setStatus",
      "statusKey":"pi-mcp/live-v1","statusText":json.dumps(receipt)}})
""",
    )
    # A supplied, not-yet-set finish_event leaves stats_requested false at the
    # settled yield: this used to admit a late status after ACP TurnSettled.
    events = [
        event
        async for event in backend.stream_agent_events(
            program, [], "fixture", str(tmp_path), finish_event=asyncio.Event()
        )
    ]
    assert any(isinstance(event, ae.StreamSettled) for event in events)
    assert not any(isinstance(event, ae.McpLiveStatus) for event in events)


async def test_explicit_owner_cancellation_is_not_swallowed_by_socket_permission():
    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

    subscriber = SocketClient(Writer())
    pending = asyncio.create_task(subscriber.permission({"toolCall": {}, "options": []}))
    for _ in range(10):
        if subscriber.pending:
            break
        await asyncio.sleep(0)
    assert subscriber.pending
    pending.cancel()  # Also exercise cancellation before the fast drain await settles.
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not subscriber.pending


async def test_private_subscriber_token_routes_only_active_prompt_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/z-ai/glm-5.3-flash")
    owner = canonical_agent(wire(tmp_path / "wire"), runtime_enabled=True, auto_wake=False)
    await owner.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    session_id = "project"
    calls = [[], []]
    proxies = []
    try:
        for index in range(2):

            async def update(**kwargs):
                pass

            async def answer(*, position=index, **kwargs):
                calls[position].append(kwargs)
                return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

            instance = SimpleNamespace(session_update=update, request_permission=answer)
            fake = SimpleNamespace(
                _comms=owner._comms,
                sessions=SimpleNamespace(
                    transcript=SimpleNamespace(snapshots=False, diffs=False), client=instance
                ),
            )
            proxy = RuntimeProxy(fake, session_id, owner._runtime.path)
            await proxy.subscribe()
            proxies.append(proxy)
        assert proxies[0]._controller_token != proxies[1]._controller_token
        assert "controllerToken" not in owner.sessions.metadata(session_id)["agentComms"]
        request = pi.ExtensionUiRequest(
            id="only-one-child",
            method="confirm",
            title="Approve once?",
            message="Exactly this request",
        )

        async def fake_prompt(session_id, prompt, **kwargs):
            controller = owner._runtime.controller.get()
            owner.turns.active_turns[session_id] = "private-turn"
            try:
                answer = await owner.turns.extension_ui_permission(
                    session_id, "private-turn", controller, request
                )
            finally:
                owner.turns.active_turns.pop(session_id, None)
            return PromptResponse(
                stop_reason="end_turn",
                field_meta={"answer": FieldCodec.encode(answer.response(request))},
            )

        monkeypatch.setattr(owner, "prompt", fake_prompt)
        result = await asyncio.wait_for(proxies[0].request("prompt", prompt=[]), timeout=4)
        assert result["_meta"]["answer"]["confirmed"] is True
        assert len(calls[0]) == 1 and not calls[1]
        assert calls[0][0]["session_id"] == session_id
        assert calls[0][0]["options"][0]["kind"] == "allow_once"
        # No subscriber may borrow another attachment's controller token.
        saved = proxies[1]._controller_token
        for absent_or_invalid in (None, "0" * 64):
            proxies[1]._controller_token = absent_or_invalid
            result = await asyncio.wait_for(proxies[1].request("prompt", prompt=[]), timeout=4)
            assert result["_meta"]["answer"]["cancelled"] is True
            assert not calls[1]
        proxies[1]._controller_token = saved
        # A controller can disappear after presentation but before answering.
        # The detached owner must deny and must not transfer the pending
        # prompt to the still-connected passive second attachment.
        entered = asyncio.Event()
        unresolved = asyncio.Event()

        async def blocked_answer(**kwargs):
            entered.set()
            await unresolved.wait()
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

        proxies[0].agent.sessions.client.request_permission = blocked_answer
        pending = asyncio.create_task(proxies[0].request("prompt", prompt=[]))
        await asyncio.wait_for(entered.wait(), timeout=4)
        await proxies[0].close()
        disconnected = await asyncio.wait_for(pending, timeout=4)
        assert disconnected["_meta"]["answer"]["cancelled"] is True
        assert not calls[1]
    finally:
        for proxy in proxies:
            await proxy.close()
        await owner.shutdown()
