"""Mounted ACP orchestration isolation, shutdown and original exception provenance."""

import asyncio
from pathlib import Path

import pytest
from acp.agent.router import build_agent_router

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.owned_turn import OwnedTurn
from agent_comms.turn_runner import TurnRunner
from delivery_owner_fixture import canonical_agent
from test_backend_native_lifecycle import native_backend as native_backend


@pytest.fixture
async def owner(comms, monkeypatch):
    agent = canonical_agent(comms, agent_bin="pi", agent_args=[], auto_wake=False)
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


@pytest.fixture
async def prepared_owner(native_backend):
    """Real saved history and attested native preparation before fault injection."""
    native = native_backend
    await native.run("Completed seed exchange")
    await native.persistent.close()
    from agent_comms.comms import Comms

    agent = canonical_agent(
        Comms(native.root),
        agent_bin="pi",
        auto_wake=False,
        agent_args=["--provider", "response-local", "--model", "fixture", "--thinking", "off"],
    )
    session = (await agent.new_session(cwd=str(native.project))).session_id
    agent._comms.threads.attach_session(session, str(native.session))
    try:
        yield agent, session, native
    finally:
        await agent.shutdown()
        assert not agent.turns.persistent_backends


def failure_facts(updates, family):
    return [
        fact
        for update in updates
        for fact in decode_updates(update.field_meta)
        if isinstance(fact, family)
    ]


@pytest.mark.parametrize(
    "prior",
    [
        None,
        events.Error("specific provider failure"),
        events.Done("specific provider failure", False),
    ],
)
async def test_uncaught_failure_feedback_once_even_after_done(prepared_owner, monkeypatch, prior):
    owner, session, native = prepared_owner
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    original = native.session.read_bytes()

    async def stream(*args, **kwargs):
        persistent = owner.turns.persistent_backends[session]
        assert persistent.available and persistent.custody.child.proc.alive()
        if prior is not None:
            yield prior
        raise RuntimeError("execution failed")

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    with pytest.raises(RuntimeError, match="execution failed"):
        await owner.prompt(session, [{"type": "text", "text": "Work"}])
    errors = failure_facts(updates, RequestFailedUpdate)
    assert len(errors) == (2 if isinstance(prior, events.Error) else 1)
    from agent_comms.input_attempt import NotSentInput

    assert errors[-1].failure.input_state is NotSentInput
    expected = prior.text if prior is not None else "execution failed"
    assert errors[-1].failure.detail == expected
    assert native.session.read_bytes() == original
    assert native.provider.posts == 1  # Preparation/fault must not send or replay input.
    assert owner._comms.registry.require(session).active_turn is None
    assert not owner.turns.turn_tasks and not owner.turns.active_turns
    assert not owner.inputs.backend_inboxes


async def test_compaction_fault_reaches_acp_client_without_original_send(
    prepared_owner, monkeypatch
):
    from agent_comms.selected_pi_summary_rpc import SelectedChildUnknown

    owner, session, native = prepared_owner
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    before = native.session.read_bytes()
    source = OSError("402: insufficient credits on this model")
    failure = SelectedChildUnknown(str(source))
    attempts = []

    async def compact(*args, **kwargs):
        assert args[4].model == "response-local/fixture"
        assert args[6].custody.child.proc.alive()
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
    errors = failure_facts(updates, RequestFailedUpdate)
    assert len(errors) == 1 and errors[0].failure.detail == str(failure)
    failed_inputs = failure_facts(updates, InputFailedUpdate)
    assert len(failed_inputs) == 1
    assert failed_inputs[0].text == "Original stays unknown"
    assert failed_inputs[0].failure == errors[0].failure
    from agent_comms.input_attempt import NotSentInput

    assert errors[0].failure.input_state is NotSentInput
    assert "Not sent" in errors[0].failure.feedback
    assert owner.inputs.dispositions.read().rows[attempts[0]].declared_name == "not_sent"
    assert native.provider.posts == 1
    assert native.session.read_bytes() == before
    assert not owner.turns.turn_tasks and not owner.turns.active_turns
    assert not owner.inputs.backend_inboxes


async def test_actual_provider_failure_reports_started_input_once_without_retry(prepared_owner):
    owner, session, native = prepared_owner
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    native.provider.status = 503
    async with asyncio.timeout(30):
        await owner.prompt(session, [{"type": "text", "text": "Actual failed input"}])
    errors = failure_facts(updates, RequestFailedUpdate)
    assert len(errors) == 1
    assert "loopback retryable failure" in errors[0].failure.detail
    assert errors[0].failure.input_state.public_status == "started"
    assert not failure_facts(updates, InputFailedUpdate)
    assert native.provider.posts == 2  # seed + failed original, zero retry
    saved = native.saved_inputs()
    assert len(saved) == 2
    # The owner adds its instruction/awareness prefix to the actual native prompt.
    assert (
        sum(
            block["text"].endswith("Actual failed input")
            for message in saved
            for block in message["content"]
            if block["type"] == "text"
        )
        == 1
    )
    assert owner._comms.registry.require(session).active_turn is None
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
