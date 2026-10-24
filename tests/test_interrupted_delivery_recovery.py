"""A following message interrupted before native send returns to selected work."""

from __future__ import annotations

from functools import partial

import pytest

from agent_comms.assignment_states import DeferredAssignment, FullPendingAssignment
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.coordination_cohort import pending_sealed_assignments
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import InputId, TurnId
from agent_comms.input_attempt import NotSentInput
from agent_comms.queued_input import DeliveredInput
from conftest import publish_initial
from stored_inputs import stored_inputs
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.mark.parametrize("native_bound", [False, True])
async def test_interrupted_following_delivery_recovers_only_before_native_send(
    tmp_path, native_bound,
):
    comms, agent, root_id = _session(tmp_path)
    owner = comms.registry.require("beta")
    message = publish_initial(comms, "sender", "beta", "must survive selection")
    store_path = comms.root / "coordination.sqlite3"
    lookup = owner.incarnation.lookup
    await _accept_visible_deliveries(
        comms.bus,
        root_id,
        store_path,
        lookup,
        owner_name=owner.name,
    )

    with Coordination(str(store_path)) as store:
        assignments = pending_sealed_assignments(store, lookup)
        deliveries = comms.bus.bus.deliveries_for_references(
            tuple(row.source for row in assignments))
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
        leased = comms.agents.begin_turn(owner.name, TurnId('9c4c7984ba66b4cb4fc11ce230b963d3'))
        assert agent.inputs.dispositions.bind(
            item.key,
            claim_scope=row.claim_generation,
            turn_id=leased.thread.require_turn_lease().turn_id,
            native_id=InputId("a" * 32),
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
        pending = pending_sealed_assignments(store, lookup)
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


async def test_recovery_without_paused_assignments_is_read_only(tmp_path, monkeypatch):
    from agent_comms.coordination_database import CoordinationStore

    comms, agent, root_id = _session(tmp_path)
    owner = comms.registry.require("beta")
    publish_initial(comms, "sender", "beta", "unbound recovery source")
    store_path = comms.root / "coordination.sqlite3"
    lookup = owner.incarnation.lookup
    await _accept_visible_deliveries(comms.bus, root_id, store_path, lookup, owner_name="beta")
    with Coordination(str(store_path)) as store:
        assignments = pending_sealed_assignments(store, lookup)
        deliveries = comms.bus.bus.deliveries_for_references(
            tuple(row.source for row in assignments))
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
