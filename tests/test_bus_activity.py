"""Activity and viewer clocks are read-only projections of committed bus appends."""
from dataclasses import replace

import pytest

from agent_comms.bus import PublicRecord
from agent_comms.bus_activity_index import ChannelActivity
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
