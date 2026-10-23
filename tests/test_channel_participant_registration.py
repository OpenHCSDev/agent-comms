"""Real publication must register the entire audience, without test SQL setup."""

from agent_comms.active_route import resolve_comms_route

from bus_record_fixture import prepare_native_comms

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.coordination_tables.participants import Participants
from agent_comms.coordinator import Coordination
from agent_comms.thread_status import StoppedThreadStatus


@pytest.mark.asyncio
async def test_channel_with_unstarted_and_stopped_subscribers_delivers_whole_cohort(tmp_path):
    comms = prepare_native_comms(Comms(resolve_comms_route(tmp_path / "wire")))
    root_id = comms.registry.prepare_native_root().namespace
    for name in ("sender", "receiver", "stopped-reviewer", "unstarted-reviewer"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    stopped = comms.registry.require("stopped-reviewer")
    comms.registry.register(stopped, StoppedThreadStatus())
    receiver = comms.registry.require("receiver")
    lookup = RecordedThreadIncarnation("fixture", receiver.created_at).lookup
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        with store.session.read():
            assert Participants.select(store.session._connection) == []
        message = comms.messaging.send("sender", "#team", "Hello @receiver").message
        initial = comms.bus.bus.delivery(message.seq)
        assert {r.canonical_thread for r in initial.audience.recipients} == {
            "receiver", "stopped-reviewer", "unstarted-reviewer"
        }
        cursor = await _accept_visible_deliveries(
            comms.bus, root_id, store.session.path, lookup, 0, owner_name=receiver.name
        )
        assert cursor == message.seq
        assignments = sealed_cohort_assignments(store, lookup)
        assert len(assignments) == 1 and assignments[0].recipient == receiver.name
        assert assignments[0].wire_seq == message.seq
        before = tuple(store.participants.get(r.recipient_lookup)
                       for r in initial.audience.recipients)
        again = comms.messaging.send("sender", "#team", "Second @receiver").message
        assert await _accept_visible_deliveries(
            comms.bus, root_id, store.session.path, lookup, cursor, owner_name=receiver.name
        ) == again.seq
        assert tuple(store.participants.get(r.recipient_lookup)
                     for r in initial.audience.recipients) == before
        assert len(sealed_cohort_assignments(store, lookup)) == 2
        assert comms.registry.snapshot().statuses[stopped.name].stopped
        assert all(not thread.process_alive for thread in comms.registry.snapshot().threads.values())


