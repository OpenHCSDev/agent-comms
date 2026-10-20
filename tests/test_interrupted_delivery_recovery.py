"""A following message interrupted before native send returns to selected work."""

from __future__ import annotations

from conftest import publish_initial
import asyncio
import threading
from functools import partial

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.assignment_states import DeferredAssignment, FullPendingAssignment
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.coordination_cohort import pending_sealed_assignments
from agent_comms.coordinator import Coordination
from agent_comms.input_attempt import NotSentInput
from agent_comms.queued_input import DeliveredInput
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import tmp_path as private_root_fixture
from stored_inputs import stored_inputs


tmp_path = private_root_fixture


@pytest.mark.parametrize("native_bound", [False, True])
async def test_interrupted_following_delivery_recovers_only_before_native_send(
    tmp_path, native_bound,
):
    comms, agent, root_id = _session(tmp_path)
    owner = comms.registry.require("beta")
    message = publish_initial(comms, "sender", "beta", "must survive selection")
    store_path = comms.root / "coordination.sqlite3"
    lookup = RecordedThreadIncarnation("fixture", owner.created_at).lookup
    await _accept_visible_deliveries(
        comms.bus, root_id, store_path, lookup, 0, owner_name=owner.name,
    )

    with Coordination(str(store_path)) as store:
        assignments = pending_sealed_assignments(store, lookup, owner.name)
        with comms.bus.log.checked_read() as source:
            deliveries = tuple(source.references(tuple(row.source for row in assignments)))
    # Freeze the original storage operation without placing its live result,
    # representing an owner that died after reservation and before queue handoff.
    reserved = []
    await Coordination.run_worker(partial(
        DeliveredInput.reserve_pending, agent.inputs, owner.name, root_id,
        tuple(zip(deliveries, assignments, strict=True)), (), reserved,
    ))
    assert len(reserved) == 1
    with Coordination(str(store_path)) as store:
        paused = store.assignments.get(assignments[0].assignment_id)
    assert isinstance(paused.lifecycle, DeferredAssignment)
    assert paused.lifecycle.execution_id is None

    item, _content = reserved[0]
    row = stored_inputs(agent.inputs.dispositions.root).lookup(item.key)
    assert not row.has_native_binding
    if native_bound:
        leased = comms.agents.begin_turn(owner.name, "interrupted-turn")
        assert agent.inputs.dispositions.bind(
            item.key,
            claim_scope=row.claim_scope,
            turn_id=leased.thread.require_turn_lease().turn_id,
            native_id="a" * 32,
            text=row.source_text,
        )
        assert comms.agents.finish_turn(leased.turn_lease)

    if not native_bound:
        agent.inputs.state(owner.name).queued = {item.input_id: item}
        assert await DeliveredInput.recover_interrupted(
            agent.inputs, owner.name, root_id,
        ) == 0
        agent.inputs.state(owner.name).queued = {}

    recovered = await DeliveredInput.recover_interrupted(
        agent.inputs, owner.name, root_id,
    )
    with Coordination(str(store_path)) as store:
        current = store.assignments.get(paused.assignment_id)
        pending = pending_sealed_assignments(store, lookup, owner.name)
    if native_bound:
        assert recovered == 0
        assert isinstance(current.lifecycle, DeferredAssignment)
        assert not pending
    else:
        assert recovered == 1
        assert isinstance(current.lifecycle, FullPendingAssignment)
        assert tuple(row.assignment_id for row in pending) == (current.assignment_id,)
        assert isinstance(stored_inputs(agent.inputs.dispositions.root).lookup(item.key), NotSentInput)

    assert message.seq == current.wire_seq
    await agent.shutdown()


async def test_empty_delivery_recovery_opens_no_coordinator(tmp_path, monkeypatch):
    comms, agent, root_id = _session(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("empty recovery must not open SQLite")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Coordination, "__init__", forbidden)
            assert await DeliveredInput.recover_interrupted(agent.inputs, "beta", root_id) == 0
    finally:
        await agent.shutdown()


async def test_selected_phase_and_delivery_share_wire_without_blocking_loop(tmp_path, monkeypatch, *, cursor_delivery):
    from agent_comms.selected_participant import SelectedParticipant
    from agent_comms.turn_input_source import SelectedOriginalInput
    from agent_comms.turn_phase import PromptAcceptancePhase

    comms, agent, root_id = _session(tmp_path)
    original = publish_initial(comms, "sender", "beta", "selected batch")
    owner = comms.registry.require("beta")
    path = comms.root / "coordination.sqlite3"
    lookup = RecordedThreadIncarnation("fixture", owner.created_at).lookup
    await _accept_visible_deliveries(comms.bus, root_id, path, lookup, 0, owner_name="beta")
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    capacity = DeliveredInput.remaining_capacity

    def hold_inside_delivery(inputs, sources):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return capacity(inputs, sources)

    monkeypatch.setattr(DeliveredInput, "remaining_capacity", staticmethod(hold_inside_delivery))
    try:
        with Coordination(str(path)) as store:
            async with SelectedParticipant.select(
                comms, store, root_id, "beta", 0, cursor_delivery=cursor_delivery
            ) as selected:
                assert selected is not None
                leased = selected.owner.thread
                inbox = agent.inputs.bind_native_turn("beta", leased)
                agent.inputs.state('beta').capture_first_source(SelectedOriginalInput.answering('selected batch'))
                following = await Coordination.run_worker(partial(
                    publish_initial, comms, "sender", "beta", "following message",
                ))
                await _accept_visible_deliveries(
                    comms.bus, root_id, path, lookup, original.seq, owner_name="beta",
                )
                delivery = asyncio.create_task(DeliveredInput.deliver_pending(
                    agent.inputs, "beta", leased, comms.bus, root_id, original.seq,
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    # The release must run on this same loop. A synchronous
                    # phase read here would prevent the holder ever releasing.
                    timer = loop.call_later(0.05, release.set)
                    await asyncio.wait_for(asyncio.gather(
                        selected.transition(PromptAcceptancePhase()), delivery,
                    ), 5)
                    timer.cancel()
                    assert type(comms.registry.require("beta").active_turn.phase) is PromptAcceptancePhase
                    (placed,) = agent.inputs.state("beta").queued.values()
                    assert placed.message == following and inbox.qsize() == 1
                finally:
                    release.set()
                    await asyncio.gather(delivery, return_exceptions=True)
                    await agent.inputs.finish_turn_inputs("beta", inbox)
    finally:
        release.set()
        await agent.shutdown()


async def test_recovery_without_paused_assignments_is_read_only(tmp_path, monkeypatch):
    from agent_comms.coordination_database import CoordinationStore

    comms, agent, root_id = _session(tmp_path)
    owner = comms.registry.require("beta")
    publish_initial(comms, "sender", "beta", "unbound recovery source")
    store_path = comms.root / "coordination.sqlite3"
    lookup = RecordedThreadIncarnation("fixture", owner.created_at).lookup
    await _accept_visible_deliveries(comms.bus, root_id, store_path, lookup, 0, owner_name="beta")
    with Coordination(str(store_path)) as store:
        assignments = pending_sealed_assignments(store, lookup, "beta")
        with comms.bus.log.checked_read() as source:
            deliveries = tuple(source.references(tuple(row.source for row in assignments)))
    reserved = []
    await Coordination.run_worker(partial(
        DeliveredInput.reserve_pending, agent.inputs, "beta", root_id,
        tuple(zip(deliveries, assignments, strict=True)), (), reserved,
    ))
    (item, _content), = reserved
    with Coordination(str(store_path)) as store:
        store.assignments.restore_unbound_delivery(item.assignment)
    def forbidden(*args, **kwargs):
        raise AssertionError("a read must not open a writer")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(CoordinationStore, "_open_connection", forbidden)
            assert await DeliveredInput.recover_interrupted(agent.inputs, "beta", root_id) == 0
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("retire_inbox", [False, True])
async def test_delivery_worker_retires_unplaced_inputs_after_cancellation_or_inbox_close(
    tmp_path, monkeypatch, retire_inbox,
):
    from agent_comms.turn_input_source import SelectedOriginalInput

    comms, agent, root_id = _session(tmp_path)
    leased = comms.agents.begin_turn("beta", "delivery-worker-turn")
    inbox = agent.inputs.bind_native_turn("beta", leased.thread)
    agent.inputs.state('beta').capture_first_source(SelectedOriginalInput.answering('live selected work'))
    message = publish_initial(comms, "sender", "beta", "worker-held delivery")
    path = comms.root / "coordination.sqlite3"
    lookup = RecordedThreadIncarnation("fixture", leased.thread.created_at).lookup
    await _accept_visible_deliveries(comms.bus, root_id, path, lookup, 0, owner_name="beta")
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    main_thread = threading.get_ident()
    threads = []
    reserve = DeliveredInput.reserve_pending
    def held_reserve(*args):
        threads.append(threading.get_ident())
        reserve(*args)
        loop.call_soon_threadsafe(entered.set)
        release.wait()
    monkeypatch.setattr(DeliveredInput, "reserve_pending", held_reserve)
    delivery = asyncio.create_task(DeliveredInput.deliver_pending(
        agent.inputs, "beta", leased.thread, comms.bus, root_id, 0,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert threads and threads != [main_thread]
        if retire_inbox:
            agent.inputs.state('beta').retire_turn(agent.inputs.state('beta').require_inbox())
        else:
            delivery.cancel()
        release.set()
        if retire_inbox:
            await delivery
        else:
            with pytest.raises(asyncio.CancelledError):
                await delivery
        assert inbox.empty() and not agent.inputs.state("beta").queued
        with Coordination(str(path)) as store:
            (current,) = pending_sealed_assignments(store, lookup, "beta")
        assert current.wire_seq == message.seq
        assert isinstance(current.lifecycle, FullPendingAssignment)
        assert isinstance(
            stored_inputs(agent.inputs.dispositions.root).lookup(
                agent.inputs.dispositions.bus_key(message, leased.thread)
            ), NotSentInput,
        )
    finally:
        release.set()
        await asyncio.gather(delivery, return_exceptions=True)
        await agent.inputs.finish_turn_inputs("beta", inbox)
        comms.agents.finish_turn(leased.turn_lease)
        await agent.shutdown()
