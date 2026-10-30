"""Activity and viewer clocks are read-only projections of committed bus appends."""
from agent_comms.activity_clocks import ChannelActivity
from dataclasses import replace

import pytest

from agent_comms.bus import PublicRecord
from agent_comms.mentions import ThreadMention
from agent_comms.messages import InfoKind, Message, NoticeRole
from agent_comms.read_basis import (
    AllChannelDisplayScope, ChannelDisplayScope, NamedActivityDisplayScope,
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
        (NamedActivityDisplayScope("#member", participants=frozenset({"mentioned"})),
         ChannelActivity(20.0, 20.0)),
        (NamedActivityDisplayScope("#member", participants=frozenset({"worker"})),
         ChannelActivity(20.0, 20.0)),
        (NamedActivityDisplayScope("#member", frozenset({"#team"}),
                                  participants=frozenset({"human"})),
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


def test_read_capture_defers_when_coordinator_has_no_snapshot(private_comms):
    """A coordinator table read reports contention without claiming absence."""
    import sqlite3
    import threading
    import time

    from agent_comms.coordination_errors import CoordinationReadUnavailable
    from agent_comms.coordination_session import ReadNativeCoordinationSession
    from agent_comms.coordinator import Coordination
    from agent_comms.field_codec import ThreadLookup
    from agent_comms.store_files import StoreBusy
    held, release = threading.Event(), threading.Event()
    path = private_comms.root / "coordination.sqlite3"
    with Coordination(str(path)):
        pass

    def hold():
        with sqlite3.connect(path, isolation_level=None) as db:
            db.execute("BEGIN EXCLUSIVE")
            held.set()
            release.wait(10)
            db.execute("ROLLBACK")

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        begun = time.monotonic()
        with pytest.raises(CoordinationReadUnavailable), Coordination(
            str(path), contention=StoreBusy(), session_type=ReadNativeCoordinationSession,
        ) as store:
            store.participants.get(ThreadLookup("b" * 32))
        assert time.monotonic() - begun < 5
    finally:
        release.set()
        holder.join(5)


def test_open_boundary_never_holds_the_registry_against_its_writers(private_comms):
    """An open native boundary keeps only its registry copy; a writer acquires at
    once, and a write from the stale copy refuses instead of publishing over it."""
    import threading

    from agent_comms.coordination_errors import StaleFence
    from agent_comms.coordinator import Coordination
    from agent_comms.store_files import RegistryLocking

    registry = private_comms.bus._registry.store
    acquired = []

    def write():
        with RegistryLocking.write_guard(registry.path):
            registry.save_unlocked(registry.read_revision_unlocked().document.copy())
            acquired.append(True)

    with Coordination(str(private_comms.root / "coordination.sqlite3")) as store, \
            store.session.capture(store.session.bus) as boundary:
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(1)
        assert acquired == [True]
        with pytest.raises(StaleFence, match="registry revision changed"), boundary.registry_cut.current():
            pass
