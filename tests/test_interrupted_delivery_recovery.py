"""A following message interrupted before native send returns to selected work."""

from __future__ import annotations

import pytest

from agent_comms.assignment_states import DeferredAssignment, FullPendingAssignment
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.coordination_cohort import pending_sealed_assignments
from agent_comms.coordinator import Coordination
from agent_comms.input_attempt import NotSentInput
from agent_comms.queued_input import DeliveredInput
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import tmp_path as private_root_fixture


tmp_path = private_root_fixture


@pytest.mark.parametrize("native_bound", [False, True])
async def test_interrupted_following_delivery_recovers_only_before_native_send(
    tmp_path, native_bound,
):
    comms, agent, root_id = _session(tmp_path)
    owner = comms.registry.require("beta")
    message = comms.messaging.send_initial_cohort("sender", "beta", "must survive selection")
    store_path = comms.root / "coordination.sqlite3"
    lookup = stable_thread_lookup(owner.created_at)
    await _accept_visible_deliveries(
        comms.bus, root_id, store_path, lookup, 0, owner_name=owner.name,
    )

    with Coordination(str(store_path)) as store:
        assignments = pending_sealed_assignments(store, lookup, owner.name)
        with comms.bus.log.certified_read() as source:
            deliveries = tuple(source.references(tuple(row.source for row in assignments)))
        reserved = DeliveredInput.reserve_pending(
            agent.inputs, owner.name, root_id,
            tuple(zip(deliveries, assignments, strict=True)), store,
        )
        assert len(reserved) == 1
        paused = store.assignments.get(assignments[0].assignment_id)
        assert isinstance(paused.lifecycle, DeferredAssignment)
        assert paused.lifecycle.execution_id is None

    item, _content = reserved[0]
    row = agent.inputs.dispositions.lookup(item.key)
    assert not row.has_native_binding
    if native_bound:
        leased = comms.agents.begin_turn(owner.name, "interrupted-turn")
        assert agent.inputs.dispositions.bind(
            item.key,
            admission=row.admission,
            turn_id=leased.thread.require_turn_lease().turn_id,
            native_id="a" * 32,
            text=row.source_text,
        )
        assert comms.agents.finish_turn(leased.turn_lease)

    if not native_bound:
        agent.inputs.queued_inputs[owner.name] = {item.input_id: item}
        assert await DeliveredInput.recover_interrupted(
            agent.inputs, owner.name, root_id,
        ) == 0
        agent.inputs.queued_inputs.pop(owner.name)

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
        assert isinstance(agent.inputs.dispositions.lookup(item.key), NotSentInput)

    assert message.seq == current.wire_seq
    await agent.shutdown()
