"""Incarnation creation prepares an audience that publication only addresses."""


from agent_comms.repo import Checkout
from bus_record_fixture import prepare_native_comms

import pytest

from agent_comms.cohort_schema import AcceptanceFrontier
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.comms import wire
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.coordination_tables.participants import Participants
from agent_comms.coordinator import Coordination
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread

@pytest.mark.asyncio
async def test_channel_with_unstarted_and_stopped_subscribers_delivers_whole_cohort(tmp_path):
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    root_id = comms.registry.prepare_native_root().namespace
    for name in ("sender", "receiver", "stopped-reviewer", "unstarted-reviewer"):
        comms.registry.declare(Thread(name, frozenset({'team'}), Checkout.from_wire(str(tmp_path))))
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
        assert {r.canonical_thread for r in initial.recipients} == {
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
                       for r in initial.recipients)
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
                     for r in initial.recipients) == before
        assert len(sealed_cohort_assignments(store, lookup)) == 2
        assert comms.registry.snapshot().statuses[stopped.name].stopped
        assert all(not thread.process_alive for thread in comms.registry.snapshot().threads.values())
