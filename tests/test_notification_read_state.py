from dataclasses import replace

from agent_comms.presentation import NoRevision, WireRevision
from agent_comms.ui_model.observation import (
    NotificationsRead, ReadNotifications, ReadRecordedNotifications, UnreadableNotificationsRead,
)


def test_notification_read_states_use_the_request_revision_contract():
    original = WireRevision.empty()
    later = replace(original, expiry_tick=original.expiry_tick + 60)
    live = ReadNotifications("root", ())
    recorded = ReadRecordedNotifications("root", (), "archive")
    settled = NotificationsRead(original, {})
    assert not live.changed_since(None, original, settled)
    assert live.changed_since(None, later, settled)
    assert not recorded.changed_since(None, later, settled)
    assert live.changed_since(None, later, UnreadableNotificationsRead(original))
    assert not live.changed_since(None, original, UnreadableNotificationsRead(original))


def test_first_revision_requires_reads_even_for_unchanged_empty_stores():
    unseen = NoRevision()
    known = WireRevision.empty()
    assert unseen.stores_changed_since(known)
    assert unseen.registrations_changed_since(known)
    assert known.registrations_changed_since(unseen)
    assert unseen.registrations_changed_since(unseen)
    assert unseen.sidebar_changed_since(known)
    assert not known.stores_changed_since(known)
    assert not known.registrations_changed_since(known)
    assert not known.sidebar_changed_since(known)
