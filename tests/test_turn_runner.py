"""Mounted ACP orchestration isolation, shutdown and original exception provenance."""

import asyncio

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.acp_failure import PromptFailureReceipt
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.turn_runner import TurnRunner
from test_backend_native_lifecycle import native_backend as native_backend


@pytest.fixture
async def prepared_owner(native_backend):
    """SDK-authored history and original idle native custody; zero seed requests."""
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
        assert not any(owner.turns.turn_state(sid).busy for sid in (first, second))
        assert not owner.inputs.backend_inboxes
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
    original = native.session.read_bytes()
    source = OSError("native diagnostic source")
    failure = RuntimeError("selected operation failed")

    async def stream(*args, **kwargs):
        # This boundary fails before a native write. Preparation and the lease
        # have already been acquired by the real saved-session owner.
        assert owner.turns.turn_state(session).busy
        assert owner.turns.persistent_backends[session].available
        raise failure from source
        yield

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    with pytest.raises(RequestError) as caught:
        await owner.prompt(session, [{"type": "text", "text": "Work"}])
    assert caught.value.__cause__ is failure and failure.__cause__ is source
    receipt = PromptFailureReceipt.from_error(caught.value.code, str(caught.value), caught.value.data)
    assert receipt.notification_published and receipt.failure.input_state.public_status == "not_sent"
    assert not owner.turns.turn_tasks and not owner.turns.turn_state(session).busy
    assert not owner.inputs.backend_inboxes
    assert not owner.turns.turn_locks[session].locked()
    assert native.session.read_bytes() == original and native.provider.posts == 0


async def test_shutdown_joins_running_turn_before_releasing_session(prepared_owner):
    owner, session, native = prepared_owner
    native.provider.response_gate = asyncio.Event()
    child = owner.turns.persistent_backends[session].custody.child.proc
    turn = asyncio.create_task(owner.prompt(session, [{"type": "text", "text": "Work"}]))
    try:
        async with asyncio.timeout(30):
            while native.provider.posts != 1:
                assert not turn.done()
                await asyncio.sleep(0.01)
            assert owner.turns.turn_state(session).busy
            await owner.shutdown()
            assert (await turn).stop_reason == "cancelled"
        assert not child.alive() and not child.platform.group_members(child.identity)
        assert not owner.turns.turn_tasks
        assert not owner.turns.persistent_backends and not owner.inputs.backend_inboxes
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
        assert persistent.available and persistent.custody.child.proc.alive()
        if prior is not None:
            yield prior
        raise RuntimeError("execution failed")

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    with pytest.raises(RequestError) as caught:
        await owner.prompt(session, [{"type": "text", "text": "Work"}])
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "execution failed"
    receipt = PromptFailureReceipt.from_error(caught.value.code, str(caught.value), caught.value.data)
    errors = failure_facts(updates, RequestFailedUpdate)
    from agent_comms.input_attempt import NotSentInput, ReservedInput

    # A failed native terminal and a later controller exception carry the
    # original reservation followed by its canonical unbound settlement.
    # Equal text must not discard that distinct input evidence.
    assert [update.failure.input_state for update in errors] == (
        [ReservedInput, NotSentInput] if prior is not None else [NotSentInput]
    )
    assert receipt.notification_published and receipt.failure == errors[-1].failure
    if prior is not None:
        assert errors[-1].failure.detail == prior.text
    else:
        assert "Open diagnostic" in errors[-1].failure.detail
    assert native.session.read_bytes() == original
    assert native.provider.posts == 0  # Preparation/fault must not send or replay input.
    assert owner._comms.registry.require(session).active_turn is None
    assert not owner.turns.turn_tasks and not owner.turns.turn_state(session).busy
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
        assert args[3].model.id == "fixture"
        assert args[5].custody.child.proc.alive()
        attempts.extend(args[4])
        raise failure from source

    async def forbidden_stream(*args, **kwargs):
        pytest.fail("original input must not reach native after uncertain compaction")
        yield

    monkeypatch.setattr("agent_comms.owner_compaction_adaptive.maybe_compact_owner_turn", compact)
    monkeypatch.setattr(backend, "stream_agent_events", forbidden_stream)
    with pytest.raises(RequestError) as caught:
        await owner.prompt(session, [{"type": "text", "text": "Original stays unknown"}])
    assert caught.value.__cause__ is failure and failure.__cause__ is source
    receipt = PromptFailureReceipt.from_error(caught.value.code, str(caught.value), caught.value.data)
    assert len(attempts) == 1
    errors = failure_facts(updates, RequestFailedUpdate)
    assert len(errors) == 1 and receipt.failure == errors[0].failure
    assert receipt.notification_published and "Open diagnostic" in errors[0].failure.detail
    failed_inputs = failure_facts(updates, InputFailedUpdate)
    assert len(failed_inputs) == 1
    assert failed_inputs[0].text == "Original stays unknown"
    assert failed_inputs[0].failure == errors[0].failure
    from agent_comms.input_attempt import NotSentInput

    assert errors[0].failure.input_state is NotSentInput
    assert "Not sent" in errors[0].failure.feedback
    assert owner.inputs.dispositions.read().rows[attempts[0]].declared_name == "not_sent"
    assert native.provider.posts == 0
    assert native.session.read_bytes() == before
    assert not owner.turns.turn_tasks and not owner.turns.turn_state(session).busy
    assert not owner.inputs.backend_inboxes


async def test_actual_provider_failure_reports_started_input_once_without_retry(prepared_owner):
    owner, session, native = prepared_owner
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    native.provider.status = 503
    with pytest.raises(RequestError) as caught:
        async with asyncio.timeout(30):
            await owner.prompt(session, [{"type": "text", "text": "Actual failed input"}])
    receipt = PromptFailureReceipt.from_error(caught.value.code, str(caught.value), caught.value.data)
    errors = failure_facts(updates, RequestFailedUpdate)
    assert len(errors) == 1
    assert receipt.notification_published and receipt.failure == errors[0].failure
    assert "loopback retryable failure" in errors[0].failure.detail
    assert errors[0].failure.input_state.public_status == "started"
    assert not failure_facts(updates, InputFailedUpdate)
    assert native.provider.posts == 1  # failed original, zero seed/retry
    saved = native.saved_inputs()
    assert len(saved) == 2
    from agent_comms.native_entries import NativeEntry

    # The original tracked input, rather than the historical SDK content's
    # string/array shape, owns the exact current native message.
    (original,) = owner.inputs.dispositions.read().rows.values()
    _, entries = NativeEntry.read_evidence(native.session)
    tracked = NativeEntry.tracked_users(entries)
    assert tuple(tracked) == (original.native_id,)
    assert tracked[original.native_id].message.text == original.sent_text
    assert original.sent_text.endswith("Actual failed input")
    assert owner._comms.registry.require(session).active_turn is None
    assert not owner.turns.turn_tasks and not owner.turns.turn_state(session).busy
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
