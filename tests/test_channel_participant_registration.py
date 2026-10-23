"""Incarnation creation prepares an audience that publication only addresses."""


from bus_record_fixture import prepare_native_comms

import pytest

from agent_comms.cohort_schema import AcceptanceFrontier
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.comms import wire
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.participants import Participants
from agent_comms.coordinator import Coordination
from agent_comms.native_runtime_input import CurrentNativeCursor
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread
from cutover.carry_acceptance_frontier import AcceptanceFrontiers

@pytest.mark.asyncio
async def test_channel_with_unstarted_and_stopped_subscribers_delivers_whole_cohort(tmp_path):
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    root_id = comms.registry.prepare_native_root().namespace
    for name in ("sender", "receiver", "stopped-reviewer", "unstarted-reviewer"):
        comms.registry.declare(Thread(name, frozenset({'team'}), str(tmp_path)))
    stopped = comms.registry.require("stopped-reviewer")
    comms.registry.register(stopped, StoppedThreadStatus())
    receiver = comms.registry.require("receiver")
    lookup = receiver.incarnation.lookup
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        with store.session.read():
            assert {row.participant_lookup for row in Participants.select(store.session._connection)} == {
                thread.incarnation.lookup for thread in comms.registry.snapshot().threads.values()
            }
        message = comms.messaging.send("sender", "#team", "Hello @receiver").message
        initial = comms.bus.bus.delivery(message.seq)
        assert {r.canonical_thread for r in initial.audience.recipients} == {
            "receiver", "stopped-reviewer", "unstarted-reviewer"
        }
        await _accept_visible_deliveries(
            comms.bus, root_id, store.session.path, lookup, owner_name=receiver.name
        )
        assert (
            AcceptanceFrontier.require(
                store.session._connection,
                wire_root_id=root_id,
                recipient_lookup=lookup,
            ).through_seq
            == message.seq
        )
        assignments = sealed_cohort_assignments(store, lookup)
        assert len(assignments) == 1 and assignments[0].recipient == receiver.name
        assert assignments[0].wire_seq == message.seq
        before = tuple(store.participants.get(r.recipient_lookup)
                       for r in initial.audience.recipients)
        again = comms.messaging.send("sender", "#team", "Second @receiver").message
        await _accept_visible_deliveries(
            comms.bus, root_id, store.session.path, lookup, owner_name=receiver.name
        )
        assert (
            AcceptanceFrontier.require(
                store.session._connection,
                wire_root_id=root_id,
                recipient_lookup=lookup,
            ).through_seq
            == again.seq
        )
        assert tuple(store.participants.get(r.recipient_lookup)
                     for r in initial.audience.recipients) == before
        assert len(sealed_cohort_assignments(store, lookup)) == 2
        assert comms.registry.snapshot().statuses[stopped.name].stopped
        assert all(not thread.process_alive for thread in comms.registry.snapshot().threads.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_hole", [False, True])
async def test_carried_acceptance_frontier_reaches_new_dm_in_one_wake(tmp_path, earlier_hole):
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    root_id = comms.registry.prepare_native_root().namespace
    for name in ("sender", "receiver"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    receiver = comms.registry.require("receiver")
    lookup = receiver.incarnation.lookup
    path = comms.root / "coordination.sqlite3"
    unaccepted = []
    with Coordination(str(path)) as store:
        for index in range(102):
            if earlier_hole and index == 40:
                unaccepted.append(comms.messaging.send("sender", "receiver", "earlier DM").message)
            sealed = comms.messaging.send("sender", "receiver", f"sealed {index}").message
            accept_delivery_cohort(comms.bus, root_id, sealed.seq, store)
        unaccepted.append(comms.messaging.send("sender", "receiver", "new DM").message)
        # The pre-install store has acceptance records but no frontier row.
        with store.session.writing() as db:
            AcceptanceFrontier.delete(db, wire_root_id=root_id, recipient_lookup=lookup)

    carry = AcceptanceFrontiers(
        root=comms.root, original_root=comms.root, spill_dir=tmp_path / "spill"
    )
    assert carry.carry(apply=False).changed == 1
    assert carry.carry(apply=True).changed == 1
    with Coordination(str(path)) as store:
        assert (
            AcceptanceFrontier.require(
                store.session._connection,
                wire_root_id=root_id,
                recipient_lookup=lookup,
            ).through_seq
            == unaccepted[0].seq - 1
        )

    await _accept_visible_deliveries(comms.bus, root_id, path, lookup, owner_name=receiver.name)
    with Coordination(str(path)) as store:
        for message in unaccepted:
            assert (
                WakeAssignment.require(
                    store.session._connection,
                    recipient_lookup=lookup,
                    wire_seq=message.seq,
                ).recipient
                == receiver.name
            )
        assert (
            AcceptanceFrontier.require(
                store.session._connection,
                wire_root_id=root_id,
                recipient_lookup=lookup,
            ).through_seq
            == unaccepted[-1].seq
        )
        assert CurrentNativeCursor.select(store.session._connection, recipient_lookup=lookup) == []
