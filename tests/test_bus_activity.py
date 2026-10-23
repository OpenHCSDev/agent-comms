"""Activity and viewer clocks are read-only projections of committed bus appends."""
from agent_comms.activity_clocks import ChannelActivity
from dataclasses import replace

import pytest

from agent_comms.bus import PublicRecord
from agent_comms.mentions import ThreadMention
from agent_comms.messages import InfoKind, Message, NoticeRole
from agent_comms.read_basis import (
    AllChannelDisplayScope, ChannelDisplayScope, MemberActivityDisplayScope,
)
from agent_comms.thread_identity import UserThreadRole


def append(bus, message):
    return bus.append(lambda seq, db: PublicRecord.from_message(replace(message, seq=seq)))


def test_activity_family_tracks_original_roles_targets_and_member_scope(bus):
    append(bus, Message("author", "#team", "newest", InfoKind, timestamp=30.0))
    direct = "@mentioned direct"
    append(bus, Message("human", "worker", direct, InfoKind, timestamp=20.0,
                        sender_role=UserThreadRole,
                        mentions=ThreadMention.find(direct, lambda name: name)))
    append(bus, Message("author", "#team", "older notice", InfoKind, timestamp=10.0,
                        role=NoticeRole()))
    clocks = bus.activity_clocks()
    assert clocks.channels == {"#team": ChannelActivity(30.0, 0.0),
                               "worker": ChannelActivity(20.0, 20.0)}
    assert clocks.sent == {"author": 30.0, "human": 20.0}
    scopes = (
        (ChannelDisplayScope("#team", frozenset({"#team"})), ChannelActivity(30.0, 0.0)),
        (AllChannelDisplayScope("#any"), ChannelActivity(30.0, 20.0)),
        (MemberActivityDisplayScope("#member", participant_names=frozenset({"mentioned"})),
         ChannelActivity(20.0, 20.0)),
        (MemberActivityDisplayScope("#member", participant_names=frozenset({"worker"})),
         ChannelActivity(20.0, 20.0)),
        (MemberActivityDisplayScope("#member", frozenset({"#team"}),
                                   participant_names=frozenset({"human"})),
         ChannelActivity(30.0, 20.0)),
    )
    with bus.reading() as captured, captured.observing() as db:
        changes = db.total_changes
        for scope, expected in scopes:
            assert scope.activity(captured) == expected
        assert db.total_changes == changes
    assert not tuple(bus.path.parent.glob("bus_*json"))


def test_activity_rolls_back_with_its_original_append(bus):
    append(bus, Message("author", "#team", "committed", InfoKind, timestamp=10.0))
    before = bus.activity_clocks()
    with pytest.raises(ValueError, match="rollback"):
        with bus.writing() as db:
            PublicRecord.from_message(
                Message("author", "#team", "uncommitted", InfoKind, seq=2, timestamp=100.0),
            ).append_to(db, 2)
            raise ValueError("rollback")
    assert bus.activity_clocks() == before
    assert bus.revision() == 1


def test_read_boundary_defers_instead_of_waiting_on_a_held_bus(private_comms):
    """A read that cannot wait out another writer raises BlockingIOError from the
    activity attach, so its caller defers instead of waiting with no end."""
    import threading

    from agent_comms.coordination_session import ReadNativeCoordinationSession
    from agent_comms.coordinator import Coordination
    from agent_comms.locked_store import StoreBusy
    from agent_comms.store_files import _store_lock

    held, release, outcome = threading.Event(), threading.Event(), []

    def hold():
        with _store_lock(private_comms.bus.bus.path):
            held.set()
            release.wait(10)

    def read():
        try:
            with Coordination(
                str(private_comms.root / "coordination.sqlite3"), contention=StoreBusy(),
                session_type=ReadNativeCoordinationSession,
            ) as store, store.session.boundary():
                outcome.append("entered")
        except BlockingIOError as error:
            outcome.append(error)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(5)
    deferred = not reader.is_alive()
    release.set()
    holder.join()
    reader.join(5)
    assert deferred
    assert isinstance(outcome[0], BlockingIOError)
