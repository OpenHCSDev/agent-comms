"""Real publication must register the entire audience, without test SQL setup."""

import pytest

from agent_comms.catalog_store import ChannelCatalog
from agent_comms.registration import Registration
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.coordination_tables.participants import Participants
from agent_comms.coordinator import Coordination
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread


@pytest.mark.asyncio
async def test_channel_with_unstarted_and_stopped_subscribers_delivers_whole_cohort(tmp_path):
    comms = Comms(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
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
        initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
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


def test_send_revision_race_delivers_once_against_newer_registry(tmp_path, monkeypatch):
    """A registry write at the send's capture is decided on; none lands after it."""
    comms = Comms(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    for name in ("sender", "receiver"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    late = comms.registry.new_thread("late-reviewer", frozenset({'team'}), str(tmp_path))
    capture, decide = ChannelCatalog.reading, Registration.snapshot
    writes = []

    def write_at_capture(catalog, *sources, **options):
        if sources == (comms.registry.store,) and not writes:
            writes.append(comms.registry.register(late))
        return capture(catalog, *sources, **options)

    def refuse_write_during_decision(registry):
        if writes and len(writes) == 1:
            with pytest.raises(BlockingIOError):
                with registry.store.locked(blocking=False):
                    pass
            writes.append(None)
        return decide(registry)

    monkeypatch.setattr(ChannelCatalog, "reading", write_at_capture)
    monkeypatch.setattr(Registration, "snapshot", refuse_write_during_decision)
    message = comms.messaging.send("sender", "#team", "Hello @receiver").message
    assert len(writes) == 2
    cohort = comms.bus.log.read_delivery_cohort(root_id, message.seq)
    assert {r.canonical_thread for r in cohort.audience.recipients} == {"receiver", "late-reviewer"}
    for name in ("receiver", "late-reviewer"):
        page = comms.bus.incoming_page(name, after=0)
        assert [row.seq for row in page.messages if row.message_id == message.message_id] == [message.seq]
