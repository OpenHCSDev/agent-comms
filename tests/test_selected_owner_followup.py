"""Selected execution and ACP followups share one live input lifetime."""

import asyncio
from dataclasses import replace

import pytest
from acp import RequestError

from agent_comms import agent_events as events
from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.acp_extension import InputDeliveryChangedUpdate, decode_updates
from agent_comms.input_attempt import NotSentInput, ReservedInput
from agent_comms.native_arguments import NativeArguments
from agent_comms.tracked_turn import TrackedTurnSession
from test_acp_private_nk_delivery import _session
from test_acp_private_nk_delivery import tmp_path as private_root_fixture
from test_coordinated_runtime import _fake_model

tmp_path = private_root_fixture


def accepted_input(response):
    receipt = next(
        update
        for update in decode_updates(response.field_meta)
        if isinstance(update, InputDeliveryChangedUpdate)
    )
    assert receipt.input_id is not None
    return receipt


@pytest.mark.parametrize("goal_mode", ["none", "active", "standby"])
@pytest.mark.parametrize("native_ok", [True, False])
async def test_selected_turn_accepts_and_starts_fresh_input_once(
    tmp_path, monkeypatch, goal_mode, native_ok
):
    comms, agent, _ = _session(tmp_path)
    agent.turns.adaptive_compaction_enabled = False
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, _ = _fake_model()
    entered, release = asyncio.Event(), asyncio.Event()
    starts = []
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

    async def blocked(package, **kwargs):
        entered.set()
        await release.wait()
        return await fake(package, **kwargs)

    async def native(*args, **kwargs):
        native_id = "b" * 32
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed is True
        assert kwargs["native_start"](None, native_id, args[2])
        starts.append(args[2])
        yield events.InputStarted(id=None)
        yield events.StreamSettled()
        yield events.Done(ok=native_ok, text="Fresh owner input completed")

    monkeypatch.setattr(TrackedTurnSession, "execute", blocked)
    monkeypatch.setattr("agent_comms.backend.stream_agent_events", native)
    comms.messaging.send_message("sender", "beta", "Selected direct message")
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert comms.registry.require("beta").active_turn is not None
        response = await asyncio.wait_for(
            agent.prompt("beta", [{"type": "text", "text": "Fresh owner input"}]), 2
        )
        receipt = accepted_input(response)
        accepted = agent.inputs.dispositions.read().rows[f"acp:{receipt.input_id}"]
        assert isinstance(accepted, ReservedInput) and not accepted.has_native_binding
        assert not starts
        release.set()
        if native_ok:
            await asyncio.wait_for(turn, 8)
        else:
            from agent_comms.acp_failure import PromptFailureReceipt

            with pytest.raises(RequestError) as failed:
                await asyncio.wait_for(turn, 8)
            assert failed.value.__cause__ is not None
            receipt_failure = PromptFailureReceipt.from_error(
                failed.value.code, str(failed.value), failed.value.data
            )
            assert receipt_failure.notification_published
            assert receipt_failure.failure.input_state is type(
                agent.inputs.dispositions.read().lookup(f"acp:{receipt.input_id}")
            )
        assert len(starts) == 1 and "Fresh owner input" in starts[0]
        row = agent.inputs.dispositions.read().rows[f"acp:{receipt.input_id}"]
        assert not row.unresolved and row.native_id == "b" * 32
        assert not agent.inputs.queued_inputs.get("beta")
        assert "beta" not in agent.inputs.backend_inboxes
        assert "beta" not in agent.turns.turn_tasks
        assert comms.registry.require("beta").active_turn is None
        assert comms.registry.require("beta").goal == before_goal
        # The completed selected reply consumes its certified dependency wait;
        # a queued owner input keeps its admission/goal authority across that change.
        assert comms.goals.goal_wait("beta") == (None if goal_mode == "standby" else before_wait)
        assert not (comms.root / "goal-private").exists()
    finally:
        release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await agent.shutdown()


@pytest.mark.parametrize("change", ["clear", "missing_key", "foreign_key", "cancel"])
async def test_selected_pending_input_never_replays_unknown(tmp_path, monkeypatch, change):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
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
    admission = comms.registry.snapshot().admission_generations["beta"]
    old_key = "acp:historical-uncertain"
    agent.inputs.dispositions.record(
        old_key, seq=None, owner="beta", admission=admission, target="beta", text="Do not replay"
    )
    old_row = agent.inputs.dispositions.read().rows[old_key]
    comms.messaging.send_message("sender", "beta", "Selected direct message")
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await asyncio.wait_for(
            agent.prompt("beta", [{"type": "text", "text": "Fresh input"}]), 2
        )
        input_id = accepted_input(response).input_id
        if change == "clear":
            await agent.inputs.clear_queued_inputs("beta")
        elif change == "missing_key":
            agent.inputs.following_sources["beta"].pop(input_id)
        elif change == "foreign_key":
            agent.inputs.following_sources["beta"][input_id] = replace(
                agent.inputs.following_sources["beta"][input_id], keys=(old_key,)
            )
        else:
            turn.cancel()
        release.set()
        if change == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, 8)
        else:
            await asyncio.wait_for(turn, 8)
        rows = agent.inputs.dispositions.read().rows
        assert rows[old_key] == old_row
        assert rows[f"acp:{input_id}"].accepts_reservation
        assert "beta" not in agent.inputs.backend_inboxes
        assert "beta" not in agent.turns.turn_tasks
    finally:
        release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await agent.shutdown()


@pytest.mark.parametrize(
    "change",
    [
        "goal_revision",
        "goal_replaced",
        "goal_paused",
        "wait_cleared",
        "owner_replaced",
        "owner_stopped",
        "missing_key",
        "foreign_key",
    ],
)
async def test_selected_handoff_rechecks_authority_at_native_write(tmp_path, monkeypatch, change):
    from dataclasses import replace

    from agent_comms.goal_actions import (
        ActiveGoalAction,
        GoalPrecondition,
        PausedGoalAction,
        SetGoalAction,
        StandbyGoalAction,
    )
    from agent_comms.owned_turn import OwnedTurn

    comms, agent, _ = _session(tmp_path)
    agent.turns.adaptive_compaction_enabled = False
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, _ = _fake_model()
    entered, release = asyncio.Event(), asyncio.Event()
    goal = comms.goals.update_goal("beta", SetGoalAction(text="Preserve this goal"))
    comms.agents.begin_turn("sender", "dependency")
    comms.goals.update_goal(
        "beta", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("sender",))
    )
    expected = {}
    prepare = OwnedTurn.prepare_native

    async def mutate(execution):
        await prepare(execution)
        if change == "goal_revision":
            comms.goals.update_goal(
                "beta", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id), progress="new")
            )
        elif change == "goal_replaced":
            comms.goals.update_goal("beta", SetGoalAction(text="Replacement goal"))
        elif change == "goal_paused":
            comms.goals.update_goal(
                "beta", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id))
            )
        elif change == "wait_cleared":
            wait = comms.goals.goal_wait("beta")
            assert comms.goals.consume_goal_wait("beta", wait.wait_id)
        elif change == "owner_replaced":
            owner = comms.registry.require("beta")
            comms.registry.register(
                replace(owner, created_at=owner.created_at + 1, active_turn=None)
            )
        elif change == "owner_stopped":
            comms.registry.unregister("beta")
        elif change == "missing_key":
            agent.inputs.following_sources["beta"].pop(execution.accepted_input_id)
        elif change == "foreign_key":
            execution.original = replace(execution.original, keys=("acp:historical-uncertain",))
        expected.update(
            goal=comms.registry.require("beta").goal, wait=comms.goals.goal_wait("beta")
        )

    async def blocked(package, **kwargs):
        entered.set()
        await release.wait()
        return await fake(package, **kwargs)

    async def native(*args, **kwargs):
        with kwargs["send_boundary"](None, "e" * 32, args[2]) as allowed:
            assert allowed is False
        yield events.InputRefused(id=None)
        yield events.StreamSettled()
        yield events.Done(ok=False, text="Authority changed before send")

    monkeypatch.setattr(OwnedTurn, "prepare_native", mutate)
    monkeypatch.setattr(TrackedTurnSession, "execute", blocked)
    monkeypatch.setattr("agent_comms.backend.stream_agent_events", native)
    comms.messaging.send_message("sender", "beta", "Selected DM")
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await asyncio.wait_for(
            agent.prompt("beta", [{"type": "text", "text": "Fresh owner input"}]), 2
        )
        input_id = accepted_input(response).input_id
        release.set()
        await asyncio.wait_for(turn, 8)
        refused = agent.inputs.dispositions.read().rows[f"acp:{input_id}"]
        assert isinstance(refused, NotSentInput)
        assert refused.public_status == "not_sent" and refused.unresolved
        assert not refused.has_native_binding and not refused.has_started
        assert (
            refused.bind(
                admission=refused.admission,
                turn_id="retry",
                native_id="f" * 32,
                text=refused.source_text,
            )
            is None
        )
        assert comms.registry.require("beta").goal == expected["goal"]
        assert comms.goals.goal_wait("beta") == expected["wait"]
        assert not (comms.root / "goal-private").exists()
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
    from pathlib import Path

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
    agent._private_nk_native_package = Path(package)
    agent.turns.adaptive_compaction_enabled = False
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
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", root_id)
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", package)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    comms.messaging.send_message("sender", "beta", "First local selected instruction")
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        assert await asyncio.to_thread(entered.wait, 12), (
            repr(turn.exception()) if turn.done() else "No native request"
        )
        response = await asyncio.wait_for(
            agent.prompt("beta", [{"type": "text", "text": "Exactly one fresh owner instruction"}]),
            2,
        )
        receipt = accepted_input(response)
        accepted = agent.inputs.dispositions.read().rows[f"acp:{receipt.input_id}"]
        assert isinstance(accepted, ReservedInput) and not accepted.has_native_binding
        assert len(requests) == 1
        release.set()
        await asyncio.wait_for(turn, 18)
        row = agent.inputs.dispositions.read().rows[f"acp:{receipt.input_id}"]
        assert not row.unresolved and row.native_id
        assert len(requests) == 2
        assert "Exactly one fresh owner instruction" in json.dumps(requests[1])
        assert not agent.inputs.backend_inboxes and not agent.turns.turn_tasks
        assert comms.registry.require("beta").active_turn is None
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
    agent.turns.adaptive_compaction_enabled = False

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
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
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
        owner = comms.registry.require("beta")
        original_key = agent.inputs.original_sources["beta"].notice_keys[0]
        receipts = agent.inputs.future_inputs(owner, original_key)
        assert len(receipts) == 2
        # The adaptive compaction owner accepts the same live queued receipts.
        agent.inputs.dispositions.read().compaction_rows(owner, original_key, agent.inputs)
        first_id = original_key.removeprefix("acp:")
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed is True
        assert kwargs["native_start"](None, "a" * 32, args[2])
        started.append(first_id)
        yield events.InputStarted(id=None)
        command = kwargs["steering_queue"].get_nowait()
        second_id = command["_input_id"]
        with kwargs["send_boundary"](second_id, "b" * 32, command["message"]) as allowed:
            assert allowed is True
        assert kwargs["native_start"](second_id, "b" * 32, command["message"])
        started.append(second_id)
        yield events.InputStarted(id=second_id)
        with kwargs["send_boundary"](second_id, "c" * 32, command["message"]) as allowed:
            assert allowed is False
        yield events.StreamSettled()
        yield events.Done(ok=True, text="Both fresh inputs handled")

    monkeypatch.setattr(TrackedTurnSession, "execute", blocked)
    monkeypatch.setattr("agent_comms.backend.stream_agent_events", native)
    comms.messaging.send_message("sender", "beta", "Selected DM")
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
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
            not agent.inputs.dispositions.read().rows[f"acp:{key}"].unresolved for key in expected
        )
    finally:
        release.set()
        await asyncio.gather(turn, return_exceptions=True)
        agent.sessions.client = None
        await agent.shutdown()
