from dataclasses import replace

from agent_comms.presentation import NoRevision, RegistrationsView, SidebarView, StoresView, WireRevision
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
    assert unseen.changed_since(StoresView(), known)
    assert unseen.changed_since(RegistrationsView(), known)
    assert known.changed_since(RegistrationsView(), unseen)
    assert unseen.changed_since(RegistrationsView(), unseen)
    assert unseen.changed_since(SidebarView(), known)
    assert not known.changed_since(StoresView(), known)
    assert not known.changed_since(RegistrationsView(), known)
    assert not known.changed_since(SidebarView(), known)
