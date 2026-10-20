"""Provider-free Pi extension UI relay controls over a real detached child pipe."""

import asyncio
import json
import sys
from pathlib import Path
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest

import agent_comms.agent_events as ae
import agent_comms.pi_events as pi
from agent_comms.acp_extension import (
    McpClientAcknowledgementUpdate,
    decode_updates,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import CAPABILITY
from agent_comms.pi_payloads import McpLiveAcknowledgement
from agent_comms.owner_protocol import OwnerLine, OwnerPermissionRequest
from agent_comms.runtime import ACP_PERMISSION_TIMEOUT_SECONDS, UNBOUND_CONTROLLER, RuntimeProxy, SocketClient
from delivery_owner_fixture import canonical_agent
from native_event_host import install_event_host
from native_backend_fixture import wait_for
from pi_session_turn import one_turn_events

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable stub"),
]


@pytest.fixture
async def saved_owner(native_backend):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    async with native.open_owner() as (owner, session_id):
        yield owner, session_id, native
    assert native.session.read_bytes() == saved
    assert native.provider.posts == 0 and native.starts == []
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)


async def test_live_projection_requires_owning_acp_turn_and_session(saved_owner):
    owner, session_id, native = saved_owner
    updates = []

    class Observer:
        async def session_update(self, **kwargs):
            updates.append(kwargs)

    observer = Observer()
    acknowledgement = McpLiveAcknowledgement(1, "pi-mcp-client", "a" * 32, "running", "turn", ())
    event = ae.McpLiveStatus(acknowledgement)
    async with native.original_input(owner, session_id, "Original acknowledgement operation") as turn:
        original_id = turn.turn_id
        await owner._emit_event(session_id, event, observer, turn_id=original_id)
        assert len(updates) == 1
        assert updates[0]["session_id"] == session_id
        assert decode_updates(updates[0]["update"].field_meta) == (
            McpClientAcknowledgementUpdate(original_id, acknowledgement),
        )
    assert not owner.turns.turn_state(session_id).active.phase.busy
    await owner._emit_event(session_id, event, observer, turn_id=original_id)
    async with native.original_input(owner, session_id, "Distinct successor operation") as successor:
        assert successor.turn_id != original_id
        # A stale acknowledgement arriving FIRST in a successor turn still loses.
        await owner._emit_event(session_id, event, observer, turn_id=original_id)
        await owner._emit_event("unbound-session", event, observer, turn_id=original_id)
        await owner._emit_event(session_id, event, observer, turn_id=None)
        assert len(updates) == 1
        await owner._emit_event(session_id, event, observer, turn_id=successor.turn_id)
        assert len(updates) == 2
        assert decode_updates(updates[-1]["update"].field_meta)[0].turn_id == successor.turn_id


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
@pytest.mark.usefixtures("native_rpc_fixture")
async def test_same_child_ui_reply_is_correlated_and_denied_without_controller(
    tmp_path, method, choice, answer
):
    marker = tmp_path / "received.json"
    program = stub(
        tmp_path,
        f"""
import json, sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, next_prompt, answer_state, answer_stats
state = json.loads(sys.stdin.readline())
send({{"id": state["id"], "type": "response", "command": "get_state", "success": True,
      "data": {{"nativeInputProofCapability": {json.dumps(CAPABILITY)},
                "sessionId": "fixture-pi-session", "sessionFile": "/fixture/one.jsonl"}}}})
prompt = next_prompt()
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
send({{"type": "message_end", "message": {{"role": "assistant", "content": [], "stopReason": "stop"}}}})
send({{"type": "agent_settled"}})
answer_state({{"nativeInputProofCapability": {CAPABILITY!r}, "sessionId": "fixture-pi-session", "sessionFile": "/fixture/one.jsonl"}})
answer_stats({{"contextUsage": {{"tokens": 2}}}})
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
        async for event in one_turn_events(
            program, [], "fixture", str(cwd), ui_request=controller
        )
    ]


async def test_owner_permission_only_for_bound_live_subscriber_and_turn(saved_owner):
    agent, session_id, native = saved_owner
    request = pi.ConfirmUiRequest(id="ui-1", title="Confirm", message="One action")

    class DirectController:
        async def session_update(self, **kwargs):
            pass

        async def request_permission(self, **kwargs):
            assert kwargs["session_id"] == session_id
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

    controller = DirectController()
    agent.on_connect(controller)
    assert agent._runtime.controller.get() is UNBOUND_CONTROLLER
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

    async with AsyncExitStack() as dialogs:
        async with native.original_input(agent, session_id, "Original permission operation") as turn:
            assert await agent.turns.extension_ui_permission(
                session_id, turn.turn_id, controller, request
            ) == pi.ConfirmedUiChoice(True)
            assert await agent.turns.extension_ui_permission(
                session_id, "wrong-turn", controller, request
            ) == pi.CancelledUiChoice()
            assert await agent.turns.extension_ui_permission(
                session_id, turn.turn_id, None, request
            ) == pi.CancelledUiChoice()
            controller.request_permission = delayed
            in_flight = asyncio.create_task(agent.turns.extension_ui_permission(
                session_id, turn.turn_id, controller, request
            ))
            dialogs.push_async_callback(asyncio.gather, in_flight, return_exceptions=True)
            dialogs.callback(in_flight.cancel)
            await asyncio.wait_for(entered.wait(), timeout=1)

        async def unresponsive(**kwargs):
            await asyncio.Event().wait()

        controller.request_permission = unresponsive
        async with native.original_input(agent, session_id, "Distinct successor permission") as successor:
            release.set()
            assert await asyncio.wait_for(in_flight, timeout=1) == pi.CancelledUiChoice()
            assert await asyncio.wait_for(agent.turns.extension_ui_permission(
                session_id, successor.turn_id, controller, request
            ), timeout=ACP_PERMISSION_TIMEOUT_SECONDS + 1) == pi.CancelledUiChoice()


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

    assert McpLiveAcknowledgement.from_status(json.dumps(valid), input_id) == FieldCodec.decode(
        McpLiveAcknowledgement,
        valid,
    )
    assert McpLiveAcknowledgement.from_status(json.dumps(valid), "b" * 32) is None
    assert (
        McpLiveAcknowledgement.from_status(
            json.dumps(valid).replace('"version": 1', '"version": 1, "version": 1'),
            input_id,
        )
        is None
    )
    valid["servers"][0]["calls"] = "automatic"
    valid["servers"][0]["state"] = "denied"
    assert McpLiveAcknowledgement.from_status(json.dumps(valid), input_id) is None
    valid["servers"][0]["calls"] = "unavailable"
    valid["servers"][0]["id"] = "\x1b[2J"
    assert McpLiveAcknowledgement.from_status(json.dumps(valid), input_id) is None


@pytest.mark.usefixtures("native_rpc_fixture")
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
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, next_prompt, answer_state, answer_stats
preflight = json.loads(sys.stdin.readline())
send({{"id":preflight["id"],"type":"response","command":"get_state","success":True,
      "data":{{"nativeInputProofCapability":{json.dumps(CAPABILITY)},
              "sessionId":"fixture-session", "sessionFile":"/fixture/one.jsonl"}}}})
prompt = next_prompt()
send({{"id":prompt["id"],"type":"response","command":"prompt","success":True}})
send({{"type":"message_start","message":{{"role":"user","content":prompt["message"],
      "inputId":prompt["inputId"]}}}})
send({{"type":"message_end","message":{{"role":"assistant","content":[],"stopReason":"stop"}}}})
send({{"type":"agent_settled"}})
acknowledgement = {input_receipt!r}
acknowledgement["inputId"] = prompt["inputId"]
send({{"type":"extension_ui_request","id":"late","method":"setStatus",
      "statusKey":"pi-mcp/live-v1","statusText":json.dumps(acknowledgement)}})
answer_state({{"nativeInputProofCapability": {CAPABILITY!r}, "sessionId": "fixture-session", "sessionFile": "/fixture/one.jsonl"}})
answer_stats({{"contextUsage": {{"tokens": 2}}}})
""",
    )
    # A supplied, not-yet-set finish_event leaves stats_requested false at the
    # settled yield: this used to admit a late status after ACP TurnSettled.
    events = [
        event
        async for event in one_turn_events(
            program, [], "fixture", str(tmp_path), finish_event=asyncio.Event()
        )
    ]
    from agent_comms.turn_phase import SettlingStatsPhase

    assert any(isinstance(event, ae.NativePhaseChanged)
               and isinstance(event.phase, SettlingStatsPhase) for event in events), events
    assert isinstance(events[-1], ae.Done) and events[-1].ok, events
    assert not any(isinstance(event, ae.McpLiveStatus) for event in events)


async def test_explicit_owner_cancellation_is_not_swallowed_by_socket_permission():
    attached = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(lambda reader, writer: attached.set_result(writer), "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    server_writer = await attached
    subscriber = SocketClient(server_writer)
    try:
        pending = asyncio.create_task(subscriber.permission({"toolCallId": "call"}, []))
        for _ in range(10):
            if subscriber.pending:
                break
            await asyncio.sleep(0)
        assert subscriber.pending
        pending.cancel()  # Cancellation can precede the socket handoff's yield.
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not subscriber.pending
        assert isinstance(OwnerLine.read(await reader.readline()), OwnerPermissionRequest)
    finally:
        writer.close(); server_writer.close()
        await writer.wait_closed(); await server_writer.wait_closed()
        server.close(); await server.wait_closed()


async def test_private_subscriber_token_routes_only_active_prompt_permission(native_backend, monkeypatch):
    native = native_backend
    config = json.loads((native.config / 'models.json').read_text())
    origin = config['providers']['response-local']['baseUrl'].removesuffix('/v1')
    probe = native.project / 'controller-ui-replies.jsonl'
    install_event_host(monkeypatch, 'pi', origin, ui_probe=probe,
                       native_settings={'compaction': {'enabled': False}, 'retry': {'enabled': False}})
    # The existing SDK host emits actual confirm/select dialogs. The prompt,
    # turn, permission handler, native RPC and localhost model are not replaced.
    async with native.open_owner(runtime_enabled=True) as (owner, session_id):
        await owner._runtime.start()
        calls = [[], []]
        proxies = []
        try:
            for index in range(2):
                async def update(**kwargs):
                    pass

                async def answer(*, position=index, **kwargs):
                    calls[position].append(kwargs)
                    return {'outcome': {'outcome': 'selected', 'optionId': 'allow-once'}}

                attachment = canonical_agent(owner._comms, auto_wake=False)
                attachment.on_connect(SimpleNamespace(session_update=update, request_permission=answer))
                proxy = RuntimeProxy(attachment, session_id, owner._runtime.path)
                await proxy.subscribe()
                proxies.append(proxy)
            assert proxies[0]._controller_token != proxies[1]._controller_token
            assert 'controllerToken' not in (await owner.sessions.metadata(session_id))['agentComms']
            result = await asyncio.wait_for(proxies[0].request(
                'prompt', prompt=[{'type': 'text', 'text': 'Ask the current controller'}]), timeout=25)
            assert result['stopReason'] == 'end_turn', result
            assert json.loads(probe.read_text().splitlines()[-1])['confirmed'] is True
            assert len(calls[0]) == 2 and not calls[1]  # Native confirm and select.
            assert calls[0][0]['session_id'] == session_id
            assert calls[0][0]['options'][0].kind == 'allow_once'
            saved = proxies[1]._controller_token
            for absent_or_invalid in (None, '0' * 64):
                proxies[1]._controller_token = absent_or_invalid
                result = await asyncio.wait_for(proxies[1].request('prompt', prompt=[{
                    'type': 'text', 'text': f'No authorized controller: {absent_or_invalid}'
                }]), timeout=25)
                assert result['stopReason'] == 'end_turn', result
                assert json.loads(probe.read_text().splitlines()[-1])['confirmed'] is False
                assert not calls[1] and len(calls[0]) == 2
            proxies[1]._controller_token = saved
            entered = asyncio.Event()
            unresolved = asyncio.Event()

            async def blocked_answer(**kwargs):
                entered.set()
                await unresolved.wait()
                return {'outcome': {'outcome': 'selected', 'optionId': 'allow-once'}}

            proxies[0].agent.sessions.client.request_permission = blocked_answer
            pending = asyncio.create_task(proxies[0].request('prompt', prompt=[{
                'type': 'text', 'text': 'Disconnect while the native dialog is open'
            }]))
            try:
                await asyncio.wait_for(entered.wait(), timeout=25)
                await proxies[0].close()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                await wait_for(lambda: not owner.turns.session_busy(session_id))
                assert not calls[1]
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        finally:
            for proxy in proxies:
                await proxy.close()
