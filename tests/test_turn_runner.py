"""Mounted ACP orchestration isolation, shutdown and original exception provenance."""

import asyncio
from pathlib import Path

import pytest
from acp.agent.router import build_agent_router

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.owned_turn import OwnedTurn
from agent_comms.turn_runner import TurnRunner


@pytest.fixture
async def owner(comms, monkeypatch):
    async def models(*args):
        return []

    monkeypatch.setattr(backend, "discover_models", models)
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], auto_wake=False)
    try:
        yield agent
    finally:
        await agent.shutdown()


async def new_session(owner, tmp_path, name):
    project = tmp_path / name
    project.mkdir()
    router = build_agent_router(owner)
    response = await router("session/new", {"cwd": str(project), "mcpServers": []}, False)
    return router, response.session_id


async def test_mounted_cancel_does_not_cancel_other_session(owner, tmp_path, monkeypatch):
    router, first = await new_session(owner, tmp_path, "first")
    _, second = await new_session(owner, tmp_path, "second")
    entered = {name: asyncio.Event() for name in (first, second)}
    finish_second = asyncio.Event()
    settled = []

    async def stream(agent_bin, args, task, cwd, env, **kwargs):
        name = Path(cwd).name
        # Backend sees the bound owner callback, including its wire-lock contract.
        assert isinstance(kwargs["send_boundary"].__self__, OwnedTurn)
        assert kwargs["send_boundary"]._maintenance_wire_locked
        entered[name].set()
        try:
            if name == first:
                await asyncio.Event().wait()
            else:
                await finish_second.wait()
            yield events.Done("finished", True)
        finally:
            settled.append(name)

    monkeypatch.setattr(backend, "stream_agent_events", stream)

    def params(sid):
        return {"sessionId": sid, "prompt": [{"type": "text", "text": "Work"}]}

    one = asyncio.create_task(router("session/prompt", params(first), False))
    two = asyncio.create_task(router("session/prompt", params(second), False))
    try:
        async with asyncio.timeout(5):
            await asyncio.gather(*(event.wait() for event in entered.values()))
            assert isinstance(owner.turns, TurnRunner)
            assert set(owner.turns.turn_tasks) == {first, second}
            assert owner.turns.turn_locks[first] is not owner.turns.turn_locks[second]
            assert not {"_turn_tasks", "_active_turns", "_turn_locks"}.intersection(vars(owner))
            assert owner.turns.inputs is owner.inputs
            assert owner.turns.sessions is owner.sessions
            await router("session/cancel", {"sessionId": first}, True)
            assert (await one).stop_reason == "cancelled"
            assert second in owner.turns.active_turns and not two.done()
            finish_second.set()
            assert (await two).stop_reason == "end_turn"
        assert sorted(settled) == sorted((first, second))
        assert not owner.turns.active_turns and not owner.turns.turn_tasks
        assert not owner.inputs.backend_inboxes
        assert all(not lock.locked() for lock in owner.turns.turn_locks.values())
    finally:
        one.cancel()
        two.cancel()
        await asyncio.gather(one, two, return_exceptions=True)


async def test_turn_failure_preserves_exact_exception_cause_and_releases_state(
    owner, tmp_path, monkeypatch
):
    _, session = await new_session(owner, tmp_path, "failed")
    source = OSError("native diagnostic source")
    failure = RuntimeError("selected operation failed")

    async def stream(*args, **kwargs):
        raise failure from source
        yield  # turn stream protocol

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    with pytest.raises(RuntimeError) as caught:
        await owner.prompt(session, [{"type": "text", "text": "Work"}])
    assert caught.value is failure
    assert caught.value.__cause__ is source
    assert not owner.turns.turn_tasks and not owner.turns.active_turns
    assert not owner.inputs.backend_inboxes
    assert not owner.turns.turn_locks[session].locked()
    assert owner._comms.registry.require(session).active_turn is None


async def test_shutdown_joins_running_turn_before_releasing_session(owner, tmp_path, monkeypatch):
    _, session = await new_session(owner, tmp_path, "closing")
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def stream(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield events.Done("unreachable", True)
        finally:
            stopped.set()

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    turn = asyncio.create_task(owner.prompt(session, [{"type": "text", "text": "Work"}]))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            await owner.shutdown()
            assert stopped.is_set()
            assert (await turn).stop_reason == "cancelled"
        assert not owner.turns.turn_tasks and not owner.turns.active_turns
        assert not owner.turns.persistent_backends and not owner.inputs.backend_inboxes
        assert not owner._comms.registry.status(session).running
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)


@pytest.mark.parametrize("prior", [None, "error", "done"])
async def test_uncaught_failure_feedback_once_even_after_done(owner, tmp_path, monkeypatch, prior):
    _, session = await new_session(owner, tmp_path, "feedback")
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())

    async def stream(*args, **kwargs):
        if prior == "error":
            yield events.Error("specific provider failure")
        elif prior == "done":
            yield events.Done("specific provider failure", False)
        raise RuntimeError("execution failed")

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    with pytest.raises(RuntimeError, match="execution failed"):
        await owner.prompt(session, [{"type": "text", "text": "Work"}])
    errors = [u for u in updates if "[agent error]" in str(u)]
    assert len(errors) == 1
    expected = "specific provider failure" if prior else "execution failed"
    assert errors[0].content.text == f"[agent error] {expected}"


async def test_compaction_fault_reaches_acp_client_without_original_send(
    owner, tmp_path, monkeypatch
):
    from dataclasses import replace

    from agent_comms.selected_pi_summary_rpc import SelectedChildUnknown

    _, session = await new_session(owner, tmp_path, "compacting")
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    # No native process or provider is started. The real ACP input admission and
    # TurnRunner adaptive hook execute; only the selected operation is substituted.
    owner.turns.agent_bin = "pi"
    owner._comms.registry.register(
        replace(owner._comms.registry.require(session), session_file=str(tmp_path / "saved.jsonl"))
    )
    source = OSError("402: insufficient credits on this model")
    failure = SelectedChildUnknown("402: insufficient credits on this model")
    attempts = []

    async def compact(*args, **kwargs):
        attempts.append(args[5])
        raise failure from source

    async def forbidden_stream(*args, **kwargs):
        pytest.fail("original input must not reach native after uncertain compaction")
        yield

    monkeypatch.setattr("agent_comms.owner_compaction_adaptive.maybe_compact_owner_turn", compact)
    monkeypatch.setattr(backend, "stream_agent_events", forbidden_stream)
    with pytest.raises(SelectedChildUnknown) as caught:
        await owner.prompt(session, [{"type": "text", "text": "Original stays unknown"}])
    assert caught.value is failure and caught.value.__cause__ is source
    assert len(attempts) == 1
    errors = [u for u in updates if "[agent error]" in str(u)]
    assert len(errors) == 1
    assert errors[0].content.text == "[agent error] 402: insufficient credits on this model"
    assert errors[0].field_meta["agentComms"]["inputFailed"] == {
        "text": "Original stays unknown",
        "reason": str(failure),
    }
    assert owner.inputs.dispositions.status(attempts[0]) == "unknown"
    assert not owner.turns.turn_tasks and not owner.turns.active_turns
    assert not owner.inputs.backend_inboxes


def test_acp_has_no_superseded_component_accessors_or_dispatch():
    removed = {
        "_sessions",
        "_proxies",
        "_client",
        "_config_options",
        "_setting_requests",
        "_queued_inputs",
        "_dispositions",
        "_schedule_wake",
        "_drain_inbox",
        "_turn_tasks",
        "_turn_locks",
        "_active_turns",
        "_run_agent_turn",
        "_schedule_goal",
        "_goal_store",
        "_persistent_backends",
        "set_goal",
        "compact_context",
        "_sanitized_compaction_summary",
        "config",
    }
    assert not removed.intersection(vars(CommsAgent))
    assert not any(isinstance(value, property) for value in vars(CommsAgent).values())
