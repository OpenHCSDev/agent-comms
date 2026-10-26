import sys
from dataclasses import replace
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.history_views import HistoryViews
from agent_comms.private_path import FileRevision
from agent_comms.presentation import NoRevision, WireRevision
from agent_comms.revision_views import (
    ActivityView, NotificationEvidenceView, NotificationsView, RegistrationsView,
    SidebarView, StatusView, StoresView,
)
from agent_comms.ui_model.observation import (
    NotificationsRead, ObserveNotifications, ReadNotifications, ReadRecordedNotifications,
    UnreadableNotificationsRead,
)


def test_notification_read_states_use_the_request_revision_contract(comms):
    original = WireRevision.empty()
    later = replace(original, expiry_tick=original.expiry_tick + 60)
    live = ReadNotifications("root", ())
    recorded = ReadRecordedNotifications("root", (), "archive")
    settled = NotificationsRead(original, {})
    assert not live.changed_since(comms, original, settled)
    assert not live.changed_since(comms, later, settled)
    assert not recorded.changed_since(comms, later, settled)
    assert live.changed_since(comms, later, UnreadableNotificationsRead(original))
    assert not live.changed_since(comms, original, UnreadableNotificationsRead(original))


def test_notification_read_only_acquires_activity_when_its_source_changed(comms, tmp_path):
    marker = tmp_path / 'changed-source'
    marker.write_text('changed')
    source = FileRevision.capture(marker).revisions()
    original = WireRevision.empty()
    assert replace(original, history=source).changed_since(NotificationsView(), original)
    settled = NotificationsRead(original, {'absent-participant': None})
    calls = 0

    def count_reads(frame, event, argument):
        nonlocal calls
        if event == 'call' and frame.f_code is HistoryViews.observed_activity.__code__:
            calls += 1

    previous = sys.getprofile()
    sys.setprofile(count_reads)
    try:
        assert not settled.live_changed(comms, replace(original, reads=source))
        assert calls == 0
        assert settled.live_changed(comms, replace(original, activity=source))
        assert calls == 1
    finally:
        sys.setprofile(previous)


def test_unrelated_registry_change_keeps_the_notification_window(comms: Comms, tmp_path: Path):
    marker = tmp_path / 'registry-revision'
    marker.write_text('unrelated registration change')
    original = WireRevision.empty()
    later = replace(original, registry=FileRevision.capture(marker).revisions())
    settled = NotificationsRead(original)

    assert later.changed_since(ActivityView(), original)
    assert not settled.live_changed(comms, later)


def test_changed_notification_interest_keeps_only_retained_read_states():
    kept = ReadNotifications('kept', ())
    removed = ReadNotifications('removed', ())
    added = ReadNotifications('added', ())
    previous = ObserveNotifications(frozenset((kept, removed))).observation()
    state = NotificationsRead(WireRevision.empty())
    previous.read_states = {kept: state, removed: state}
    requested = ObserveNotifications(frozenset((kept, added))).observation()

    continued = previous.for_interest(requested)

    assert continued is requested
    assert requested.read_states == {kept: state}


def test_activity_views_receive_expiry_without_expiring_store_or_evidence_views():
    original = WireRevision.empty()
    later = replace(original, expiry_tick=60)
    for view in (ActivityView(), SidebarView(), StatusView(), NotificationsView()):
        assert later.changed_since(view, original)
    assert not later.changed_since(StoresView(), original)
    assert not later.changed_since(NotificationEvidenceView(), original)


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


def test_notification_rank_overrides_follow_the_mro_without_changing_parent_ranks():
    from agent_comms.notification_progress import (
        ActiveDelivery, DeliveryRank, SelectedReadProgress, SendingProgress,
        SentProgress, SettledDelivery, ShownToModelProgress, WaitingDelivery,
    )

    assert {rank.__module__ for rank in DeliveryRank.__subclasses__()} == {DeliveryRank.__module__}
    assert SentProgress(100).rank() == DeliveryRank.position_of(WaitingDelivery)
    assert ShownToModelProgress(100).rank() == DeliveryRank.position_of(SettledDelivery)
    assert SendingProgress(100).rank() == DeliveryRank.position_of(ActiveDelivery)
    assert SelectedReadProgress(100).rank() == DeliveryRank.position_of(ActiveDelivery)
