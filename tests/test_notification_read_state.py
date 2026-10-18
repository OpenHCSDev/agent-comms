from dataclasses import replace

from agent_comms.presentation import NoRevision, WireRevision
from agent_comms.ui_model.observation import ReadNotifications, ReadRecordedNotifications


def test_notification_read_states_use_the_request_revision_contract():
    original = WireRevision.empty()
    later = replace(original, expiry_tick=original.expiry_tick + 60)
    live = ReadNotifications("root", ())
    recorded = ReadRecordedNotifications("root", (), "archive")
    assert NoRevision().notification_read_changed(live, original)
    assert NoRevision().notification_read_changed(recorded, original)
    assert not original.notification_read_changed(live, original)
    assert original.notification_read_changed(live, later)
    assert not original.notification_read_changed(recorded, later)


def test_first_revision_requires_reads_even_for_unchanged_empty_stores():
    unseen = NoRevision()
    known = WireRevision.empty()
    assert unseen.stores_changed_since(known)
    assert unseen.registrations_changed_since(known)
    assert unseen.sidebar_changed_since(known)
    assert not known.stores_changed_since(known)
    assert not known.registrations_changed_since(known)
    assert not known.sidebar_changed_since(known)
