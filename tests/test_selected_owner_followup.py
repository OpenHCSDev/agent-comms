"""Selected execution and ACP followups share one live input lifetime."""

from agent_comms.messages import NoticeRole
import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace

import pytest
from acp import RequestError

import agent_comms.agent_events as events
import agent_comms.coordinated_runtime as coordinated_runtime
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.acp_extension import InputDeliveryChangedUpdate, decode_updates
from agent_comms.input_attempt import ClearedUnsentInput, ReservedInput, StartedInput
from agent_comms.native_arguments import NativeArguments
from agent_comms.send_outcome import AuthorityChangedSend, GrantedSend
from agent_comms.tracked_turn import TrackedTurnSession
from test_acp_private_nk_delivery import _session
from test_acp_private_nk_delivery import tmp_path as private_root_fixture
from test_coordinated_runtime import _fake_model
from stored_inputs import stored_inputs
from selected_owner_rpc_fixture import scripted_owner, recorded_request, requests
from agent_comms.channels import ANY_CHANNEL

tmp_path = private_root_fixture


@asynccontextmanager
async def selected_reply(agent, *, error=None):
    wake = agent.inputs.state("beta").wake_task
    yield wake
    with pytest.raises(error) if error is not None else nullcontext():
        await wake


def accepted_input(response):
    acknowledgement = next(
        update
        for update in decode_updates(response.field_meta)
        if isinstance(update, InputDeliveryChangedUpdate)
    )
    assert acknowledgement.input_id is not None
    return acknowledgement


@pytest.mark.parametrize("goal_mode", ["none", "active", "standby"])
@pytest.mark.parametrize("native_ok", [True, False])
async def test_selected_turn_accepts_and_starts_fresh_input_once(
    tmp_path, scripted_owner, goal_mode, native_ok
):
    comms, agent, control = scripted_owner
    if not native_ok:
        (control / "fail").touch()
    if goal_mode != "none":
        from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction

        goal = comms.goals.update_goal("beta", SetGoalAction(text="Unrelated parked goal"))
        if goal_mode == "standby":
            comms.agents.begin_turn("sender", "dependency-work")
            comms.goals.update_goal(
                "beta",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("sender",)),
            )
    before_goal = comms.registry.require("beta").goal
    before_wait = comms.goals.goal_wait("beta")

    comms.messaging.send("sender", "beta", "Selected direct message").message
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await recorded_request(control, agent)
        wake = agent.inputs.state("beta").wake_task
        assert comms.registry.require("beta").active_turn.executing
        response = await asyncio.wait_for(
            agent.prompt("beta", [{"type": "text", "text": "Fresh owner input"}]), 2
        )
        acknowledgement = accepted_input(response)
        accepted = stored_inputs(agent.inputs.dispositions.root).rows[f"acp:{acknowledgement.input_id}"]
        assert isinstance(accepted, ReservedInput) and not accepted.has_native_binding
        assert len(requests(control)) == 1
        (control / "release").touch()
        assert await asyncio.wait_for(turn, 8) == 1
        if native_ok:
            await asyncio.wait_for(wake, 12)
        else:
            from agent_comms.messages import AlertKind
            from agent_comms.native_pi import NativePiTerminalFailure
            from agent_comms.turn_failure import ModelRequestFailed
            from delivery_owner_fixture import drain_diagnostic

            # The caller got its reply when the input was queued. A later
            # model failure comes from this separately running reply task.
            with pytest.raises(NativePiTerminalFailure, match="Scripted provider failure") as failed:
                await asyncio.wait_for(wake, 12)
            assert isinstance(failed.value.failure, ModelRequestFailed)
            notice = list(comms.views.history_page(ANY_CHANNEL).messages)[-1]
            assert isinstance(notice.role, NoticeRole) and notice.type is AlertKind
            assert notice.target == "sender" and "No automatic retry" in notice.body
            assert "Scripted provider failure" in drain_diagnostic(comms, "beta").reason
        sent = requests(control)
        assert len(sent) == 2 and "Fresh owner input" in sent[1]["message"]
        row = stored_inputs(agent.inputs.dispositions.root).rows[f"acp:{acknowledgement.input_id}"]
        assert isinstance(row, StartedInput)
        assert not row.unresolved and row.native_id == sent[1]["inputId"]
        assert not agent.inputs.state("beta").queued
        assert agent.inputs.state("beta").inbox is None
        assert "beta" not in agent.turns.turn_tasks
        assert not comms.registry.require("beta").active_turn.executing
        assert comms.registry.require("beta").goal == before_goal
        # The completed selected reply consumes its certified dependency wait;
        # a queued owner input keeps its claim_scope/goal authority across that change.
        from agent_comms.goal_waits import NoGoalWait

        assert comms.goals.goal_wait("beta") == (
            NoGoalWait() if goal_mode == "standby" and native_ok else before_wait
        )
        # A real running inbox opens the goal store even without a goal.
        # The goal itself is checked above.
    finally:
        (control / "release").touch()
        await asyncio.gather(turn, return_exceptions=True)
        await agent.shutdown()


@pytest.mark.parametrize("change,expected_state", [
    ("clear", ClearedUnsentInput),
    ("missing_key", ReservedInput),
    ("foreign_key", ReservedInput),
    ("cancel", ReservedInput),
])
async def test_selected_pending_input_never_replays_unknown(tmp_path, monkeypatch, change, expected_state):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    fake, _ = _fake_model()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(package, **kwargs):
        entered.set()
        await release.wait()
        return await fake(package, **kwargs)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Revoked or historical input reached native execution")
        yield

    monkeypatch.setattr(TrackedTurnSession, "execute", blocked)
    monkeypatch.setattr("agent_comms.backend.stream_agent_events", forbidden)
    claim_scope = comms.registry.snapshot().claimed_generations["beta"]
    old_key = "acp:historical-uncertain"
    agent.inputs.dispositions.record(
        old_key, seq=None, owner="beta", claim_scope=claim_scope, target="beta", text="Do not replay"
    )
    old_row = stored_inputs(agent.inputs.dispositions.root).rows[old_key]
    comms.messaging.send("sender", "beta", "Selected direct message").message
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with selected_reply(agent, error=asyncio.CancelledError if change == "cancel" else None) as wake:
            response = await asyncio.wait_for(
                agent.prompt("beta", [{"type": "text", "text": "Fresh input"}]), 2
            )
            input_id = accepted_input(response).input_id
            if change == "clear":
                await agent.inputs.clear_queued_inputs("beta")
            elif change == "missing_key":
                agent.inputs.state("beta").following.pop(input_id)
            elif change == "foreign_key":
                agent.inputs.state("beta").following[input_id] = replace(
                    agent.inputs.state("beta").following[input_id], keys=(old_key,)
                )
            else:
                wake.cancel()
            release.set()
            assert await asyncio.wait_for(turn, 8) == 1
        rows = stored_inputs(agent.inputs.dispositions.root).rows
        assert rows[old_key] == old_row
        assert isinstance(rows[f"acp:{input_id}"], expected_state)
        assert agent.inputs.state("beta").inbox is None
        assert "beta" not in agent.turns.turn_tasks
    finally:
        release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await agent.shutdown()


async def test_actual_native_selected_and_followup_use_one_live_input_lifetime(
    tmp_path, monkeypatch
):
    """Two real pinned CLI children; deterministic loopback, no provider account."""
    import json
    import os
    import threading
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Requires existing prepared native package; never builds or calls a provider")
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            if len(requests) == 1:
                entered.set()
                assert release.wait(12)
            content = "Selected response" if len(requests) == 1 else "Fresh followup response"
            chunk = {
                "id": "local-only",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "native-fixture",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = tmp_path / "isolated-native-config"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "configured-fixture": {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "native-fixture",
                                "name": "Local fixture",
                                "contextWindow": 32768,
                                "maxTokens": 128,
                            }
                        ],
                    }
                }
            }
        )
    )
    (config / "auth.json").write_text(
        json.dumps({"configured-fixture": {"type": "api_key", "key": "localhost-only"}})
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    comms, agent, root_id = _session(tmp_path)
    comms.registry.register(
        replace(comms.registry.require("beta"), model="configured-fixture/native-fixture")
    )
    from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction

    goal = comms.goals.update_goal("beta", SetGoalAction(text="Preserve unrelated goal"))
    comms.agents.begin_turn("sender", "dependency-running")
    comms.goals.update_goal(
        "beta", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("sender",))
    )
    parked_goal = comms.registry.require("beta").goal
    parked_wait = comms.goals.goal_wait("beta")
    agent.turns.agent_args = NativeArguments.parse(
        [
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--session-dir",
            str(tmp_path / "owner-sessions"),
        ]
    )
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    comms.messaging.send("sender", "beta", "First local selected instruction").message
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        assert await asyncio.to_thread(entered.wait, 12), (
            repr(turn.exception()) if turn.done() else "No native request"
        )
        response = await asyncio.wait_for(
            agent.prompt("beta", [{"type": "text", "text": "Exactly one fresh owner instruction"}]),
            2,
        )
        acknowledgement = accepted_input(response)
        accepted = stored_inputs(agent.inputs.dispositions.root).rows[f"acp:{acknowledgement.input_id}"]
        assert isinstance(accepted, ReservedInput) and not accepted.has_native_binding
        assert len(requests) == 1
        release.set()
        await asyncio.wait_for(turn, 18)
        row = stored_inputs(agent.inputs.dispositions.root).rows[f"acp:{acknowledgement.input_id}"]
        assert not row.unresolved and row.native_id
        assert len(requests) == 2
        assert "Exactly one fresh owner instruction" in json.dumps(requests[1])
        assert not any(state.inbox for state in agent.inputs.drained.values()) and not agent.turns.turn_tasks
        assert not comms.registry.require("beta").active_turn.executing
        assert comms.registry.require("beta").goal == parked_goal
        assert comms.goals.goal_wait("beta") == parked_wait
        assert not (comms.root / "goal-private").exists()
    finally:
        release.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await agent.shutdown()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


async def test_selected_handoff_keeps_images_controller_and_future_input_receipts(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    comms, agent, _ = _session(tmp_path)

    async def session_update(**_kwargs):
        pass

    controller = SimpleNamespace(session_update=session_update)
    agent.sessions.client = controller

    async def permission(session_id, turn_id, actual_controller, request):
        assert session_id == "beta" and turn_id
        assert actual_controller is controller
        return request

    monkeypatch.setattr(agent.turns, "extension_ui_permission", permission)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    fake, _ = _fake_model()
    entered, release = asyncio.Event(), asyncio.Event()
    started = []

    async def blocked(package, **kwargs):
        entered.set()
        await release.wait()
        return await fake(package, **kwargs)

    async def native(*args, **kwargs):
        request = object()
        assert await kwargs["ui_request"](request) is request
        assert kwargs["images"][0].data == "eA=="
        original_key = agent.inputs.state("beta").first_source.notice_keys[0]
        first_id = original_key.removeprefix("acp:")
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert isinstance(allowed, GrantedSend)
        assert kwargs["native_start"](None, "a" * 32, args[2])
        started.append(first_id)
        yield events.OriginalInputStarted()
        command = kwargs["steering_queue"].get_nowait()
        second_id = command.input_id.value
        with kwargs["send_boundary"](second_id, "b" * 32, command.content.text) as allowed:
            assert isinstance(allowed, GrantedSend)
        assert kwargs["native_start"](second_id, "b" * 32, command.content.text)
        started.append(second_id)
        yield events.FollowingInputStarted(id=second_id)
        with kwargs["send_boundary"](second_id, "c" * 32, command.content.text) as allowed:
            assert isinstance(allowed, AuthorityChangedSend)
        yield events.StreamSettled()
        yield events.Done(ok=True, text="Both fresh inputs handled")

    monkeypatch.setattr(TrackedTurnSession, "execute", blocked)
    monkeypatch.setattr("agent_comms.backend.stream_agent_events", native)
    comms.messaging.send("sender", "beta", "Selected DM").message
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with selected_reply(agent):
            first = await agent.prompt(
                "beta",
                [
                    {"type": "text", "text": "First fresh input"},
                    {"type": "image", "data": "eA==", "mimeType": "image/png"},
                ],
            )
            second = await agent.prompt("beta", [{"type": "text", "text": "Second fresh input"}])
            expected = [accepted_input(response).input_id for response in (first, second)]
            release.set()
            await asyncio.wait_for(turn, 8)
        assert started == expected
        assert all(
            not stored_inputs(agent.inputs.dispositions.root).rows[f"acp:{key}"].unresolved for key in expected
        )
    finally:
        release.set()
        await asyncio.gather(turn, return_exceptions=True)
        agent.sessions.client = None
        await agent.shutdown()
