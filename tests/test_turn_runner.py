"""Mounted ACP orchestration isolation, shutdown and original exception provenance."""

import asyncio

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.acp import CommsAgent
from agent_comms.acp_failure import RequestFailure
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.turn_runner import TurnRunner
from stored_inputs import sent_text, stored_inputs


@pytest.fixture
async def prepared_owner(native_backend):
    """SDK-authored history and original idle native ownership; zero seed requests."""
    native = native_backend
    await native.author_history()
    async with native.open_owner() as (agent, session):
        yield agent, session, native


async def test_mounted_cancel_does_not_cancel_other_session(prepared_owner):
    from agent_comms.fresh_private_session import create_fresh_private_session

    owner, first, native = prepared_owner
    project = native.project / "second"
    project.mkdir()
    source = create_fresh_private_session(native.session.parent, worktree=project).path
    second = await native.bind_saved_owner(owner, project=project, session=source)
    original = native.session.read_bytes()
    native.provider.response_gate = asyncio.Event()
    router = build_agent_router(owner)

    def params(sid):
        return {"sessionId": sid, "prompt": [{"type": "text", "text": "Work"}]}

    one = asyncio.create_task(router("session/prompt", params(first), False))
    two = asyncio.create_task(router("session/prompt", params(second), False))
    try:
        async with asyncio.timeout(30):
            while native.provider.posts != 2:
                assert not one.done() and not two.done()
                await asyncio.sleep(0.01)
            assert isinstance(owner.turns, TurnRunner)
            assert set(owner.turns.turn_tasks) == {first, second}
            assert owner.turns.turn_locks[first] is not owner.turns.turn_locks[second]
            assert not {"_turn_tasks", "_active_turns", "_turn_locks"}.intersection(vars(owner))
            assert owner.turns.inputs is owner.inputs
            assert owner.turns.sessions is owner.sessions
            second_lease = owner._comms.registry.require(second).require_turn_lease()
            await router("session/cancel", {"sessionId": first}, True)
            assert (await one).stop_reason == "cancelled"
            assert owner.turns.owns_turn(second, second_lease.turn_id) and not two.done()
            native.provider.response_gate.set()
            assert (await two).stop_reason == "end_turn"
        assert not owner.turns.turn_tasks
        assert not any(owner.turns.turn_state(sid).active.phase.busy for sid in (first, second))
        assert not any(state.inbox for state in owner.inputs.drained.values())
        assert all(not lock.locked() for lock in owner.turns.turn_locks.values())
        assert native.session.read_bytes().startswith(original)
        assert len(native.saved_inputs()) == 2  # saved SDK question + one original input
        assert native.provider.posts == 2  # neither cancellation nor isolation replays
    finally:
        native.provider.response_gate.set()
        one.cancel()
        two.cancel()
        await asyncio.gather(one, two, return_exceptions=True)


async def test_turn_failure_preserves_exact_exception_cause_and_releases_state(
    prepared_owner, monkeypatch
):
    owner, session, native = prepared_owner
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    original = native.session.read_bytes()
    source = OSError("native diagnostic source")
    failure = RuntimeError("selected operation failed")

    async def stream(*args, **kwargs):
        # This boundary fails before a native write. Preparation and the lease
        # have already been acquired by the real saved-session owner.
        assert owner.turns.turn_state(session).active.phase.busy
        assert owner.turns.persistent_backends[session].available
        raise failure from source
        yield

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    with pytest.raises(RequestError) as caught:
        await owner.prompt(session, [{"type": "text", "text": "Work"}])
    assert caught.value.__cause__ is failure and failure.__cause__ is source
    request_failure = RequestFailure.from_error(caught.value)
    assert request_failure.notification_published and request_failure.failure.input_state.public_status == "not_sent"
    assert len(failure_facts(updates, RequestFailedUpdate)) == 1
    assert not owner.turns.turn_tasks and not owner.turns.turn_state(session).active.phase.busy
    assert not any(state.inbox for state in owner.inputs.drained.values())
    assert not owner.turns.turn_locks[session].locked()
    assert native.session.read_bytes() == original and native.provider.posts == 0


async def test_shutdown_joins_running_turn_before_releasing_session(prepared_owner):
    owner, session, native = prepared_owner
    native.provider.response_gate = asyncio.Event()
    child = owner.turns.persistent_backends[session].child_state.child.proc
    turn = asyncio.create_task(owner.prompt(session, [{"type": "text", "text": "Work"}]))
    try:
        async with asyncio.timeout(30):
            while native.provider.posts != 1:
                assert not turn.done()
                await asyncio.sleep(0.01)
            assert owner.turns.turn_state(session).active.phase.busy
            await owner.shutdown()
            assert (await turn).stop_reason == "cancelled"
        assert not child.alive() and not child.platform.group_members(child.identity)
        assert not owner.turns.turn_tasks
        assert not owner.turns.persistent_backends and not any(state.inbox for state in owner.inputs.drained.values())
        assert not owner._comms.registry.status(session).running
        assert len(native.saved_inputs()) == 2 and native.provider.posts == 1
    finally:
        native.provider.response_gate.set()
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)


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
        assert persistent.available and persistent.child_state.child.proc.alive()
        if prior is not None:
            yield prior
        raise RuntimeError("execution failed")

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    with pytest.raises(RequestError) as caught:
        await owner.prompt(session, [{"type": "text", "text": "Work"}])
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "execution failed"
    request_failure = RequestFailure.from_error(caught.value)
    errors = failure_facts(updates, RequestFailedUpdate)
    from agent_comms.input_attempt import NotSentInput, ReservedInput

    # A completed native failure retains its original input observation. The
    # later unbound settlement updates input delivery, not a second failure.
    # A controller fault without native Done resolves after that settlement.
    state = ReservedInput if isinstance(prior, events.Done) else NotSentInput
    assert [update.failure.input_state for update in errors] == [state]
    assert request_failure.notification_published and request_failure.failure == errors[-1].failure
    if isinstance(prior, events.Done):
        assert errors[-1].failure.detail == prior.text
    else:
        assert "Open diagnostic" in errors[-1].failure.detail
    assert native.session.read_bytes() == original
    assert native.provider.posts == 0  # Preparation/fault must not send or replay input.
    assert not owner._comms.registry.require(session).executing
    assert not owner.turns.turn_tasks and not owner.turns.turn_state(session).active.phase.busy
    assert not any(state.inbox for state in owner.inputs.drained.values())


async def test_actual_provider_failure_reports_started_input_once_without_retry(prepared_owner):
    owner, session, native = prepared_owner
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    native.provider.status = 400
    native.provider.error_message = "insufficient_quota"
    with pytest.raises(RequestError) as caught:
        async with asyncio.timeout(30):
            await owner.prompt(session, [{"type": "text", "text": "Actual failed input"}])
    request_failure = RequestFailure.from_error(caught.value)
    errors = failure_facts(updates, RequestFailedUpdate)
    assert len(errors) == 1
    assert request_failure.notification_published and request_failure.failure == errors[0].failure
    assert "insufficient_quota" in errors[0].failure.detail
    assert errors[0].failure.input_state.public_status == "started"
    assert not failure_facts(updates, InputFailedUpdate)
    assert native.provider.posts == 1  # failed original, zero seed/retry
    saved = native.saved_inputs()
    assert len(saved) == 2
    from agent_comms.native_entries import NativeEntry

    # The original tracked input, rather than the historical SDK content's
    # string/array shape, owns the exact current native message.
    (original,) = stored_inputs(owner.inputs.dispositions.root).rows.values()
    with NativeEntry.open_evidence(native.session) as evidence:
        evidence.observe()
        assert tuple(evidence.inputs) == (original.native_id,)
        assert evidence.tracked(original.native_id).message.text == sent_text(native, original)
    assert "Actual failed input" in sent_text(native, original).splitlines()
    assert not owner._comms.registry.require(session).executing
    assert not owner.turns.turn_tasks and not owner.turns.turn_state(session).active.phase.busy
    assert not any(state.inbox for state in owner.inputs.drained.values())


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
    assert {name for name, value in vars(CommsAgent).items()
            if isinstance(value, property)} == {"configured_root_id"}
