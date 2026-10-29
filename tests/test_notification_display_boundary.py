"""The combined P883 UI envelope retains display scope without granting tool authority.

These controls require the matched producer plus the two-file fc311cd64 boundary patch.
"""

from dataclasses import replace

from agent_comms.notification_reads import RecordedNotificationRead
from agent_comms.presentation import AllOutcomes
from agent_comms.ui_model.observation import ReadNotifications
from test_notification_display_viewer import archived_readers  # noqa: F401


def test_notification_request_retains_viewer_source_and_full_operator_access(archived_readers):
    fixture = archived_readers
    fixture.paint('self')
    fixture.paint('other')
    identity = fixture.selection('self').notification_identity
    other = fixture.selection('other').notification_identity
    operation = RecordedNotificationRead((fixture.message.reference,), fixture.source.key)
    request = ReadNotifications(str(fixture.live.root), operation, display=identity)
    another_viewer = replace(request, display=other)
    assert request != another_viewer
    assert len(frozenset((request, another_viewer))) == 2
    scoped = request.read(fixture.live)
    raw = fixture.notifications(AllOutcomes())
    assert scoped.outcomes == fixture.notifications(identity.outcome_viewer()).outcomes
    assert scoped.read_marks == raw.read_marks
    assert {row.recipient_identity for rows in scoped.outcomes.values() for row in rows} == {
        row.recipient_identity for rows in raw.outcomes.values() for row in rows}
    assert {reader.name for rows in scoped.outcomes.values() for row in rows
            for reader in row.displayed_to} == {'other'}
    assert {reader.name for rows in another_viewer.read(fixture.live).outcomes.values() for row in rows
            for reader in row.displayed_to} == {'self'}
    # Anonymous/operator operations still expose the raw human evidence; display is UI envelope data.
    assert ReadNotifications(str(fixture.live.root), operation).read(fixture.live).outcomes == raw.outcomes


def test_same_viewer_in_a_foreign_source_is_a_distinct_notification_request(archived_readers):
    fixture = archived_readers
    identity = fixture.selection('self').notification_identity
    live_page = fixture.live.views.channel_display_page(
        '#team', viewer=fixture.live.views.display_viewer(fixture.live.registry.require('self').worktree),
    )
    live_identity = live_page.selection.notification_identity
    assert identity.viewer == live_identity.viewer
    assert not identity.bus_identity.matches(live_identity.bus_identity)
    # Hold the rest of the captured fields fixed: source identity alone must distinguish cache keys.
    foreign = replace(identity, bus_identity=live_identity.bus_identity)
    operation = RecordedNotificationRead((fixture.message.reference,), fixture.source.key)
    request = ReadNotifications(str(fixture.live.root), operation, display=identity)
    another_source = replace(request, display=foreign)
    assert request != another_source
    assert len(frozenset((request, another_source))) == 2
    fixture.paint('self')
    raw = fixture.notifications(AllOutcomes())
    assert another_source.read(fixture.live).outcomes == raw.outcomes
    assert request.read(fixture.live).read_marks == raw.read_marks
