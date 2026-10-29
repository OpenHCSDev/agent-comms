"""Captured display provenance filters paint, never the underlying evidence or audience."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_comms.bus import Bus
from agent_comms.channel_targets import ThreadTarget
from agent_comms.comms import Comms, wire
from agent_comms.historical_views import HistorySource
from agent_comms.message_page import MessagePage
from agent_comms.messages import Message
from agent_comms.notification_evidence import NotificationWindow
from agent_comms.notification_progress import DeliveryProgress
from agent_comms.read_basis import AllOutcomes, OutcomeViewer, OwnerOutcomes
from agent_comms.presentation import MessageNotification
from agent_comms.registration import Registration
from agent_comms.read_basis import HistoricalDisplay, HistoricalSelection, NamedViewer
from agent_comms.read_ledger import ReadLedger
from agent_comms.repo import Checkout
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.threads import Thread
from bus_record_fixture import prepare_native_comms
from conftest import publish_initial


@dataclass(frozen=True)
class ArchivedReaders:
    live: Comms
    source: HistorySource
    message: Message

    def selection(self, name: str) -> HistoricalSelection:
        basis = self.live.bus.reads.capture(
            name, (self.message,), self.live.registry.snapshot(),
            Bus(Bus.path_in(Path(self.source.root))),
            document=self.live.bus.reads.read(), conversation_snapshot=self.source.provenance,
        )
        return HistoricalSelection(HistoricalDisplay(self.source, basis))

    def paint(self, name: str) -> None:
        self.selection(name).displayed.acknowledge(self.live.bus.history, self.live.registry)

    def live_page(self, name: str) -> MessagePage:
        """Read only this physical bus, not the integrated archive's nonempty page."""
        return self.live.views.presentation.channel_page('#team', viewer=NamedViewer(name))

    def notifications(self, viewer: OutcomeViewer) -> NotificationWindow:
        with self.live.bus.history.read_sources() as sources:
            return self.source.notification_references(sources, (self.message.reference,), viewer)

    @property
    def reads(self) -> ReadLedger:
        return ReadLedger.at(Path(self.source.root))


@pytest.fixture
def archived_readers(tmp_path: Path, *, thread_registration: Registration) -> ArchivedReaders:
    old = prepare_native_comms(wire(tmp_path / 'original'))
    live = prepare_native_comms(wire(tmp_path / 'current'))
    for comms in (old, live):
        for index, name in enumerate(('sender', 'self', 'other')):
            role = AgentThreadRole if comms is old or name == 'sender' else UserThreadRole
            comms.registry.declare(Thread(
                name, frozenset({'team'}), Checkout(tmp_path), created_at=10.0 + index, role=role,
            ))
    message = publish_initial(old, 'sender', '#team', 'retained audience and human paint')
    source = live.views.attach_history(old.root)
    return ArchivedReaders(live, source, message)


@pytest.mark.parametrize('painted', (('self',), ('other',), ('self', 'other')))
def test_display_subtraction_preserves_raw_other_reads_and_real_audience(
    archived_readers: ArchivedReaders, painted: tuple[str, ...],
) -> None:
    fixture = archived_readers
    for name in painted:
        fixture.paint(name)
    identity = fixture.selection('self').notification_identity
    raw_document = fixture.reads.read()
    live_document = fixture.live.bus.reads.read()
    raw = fixture.notifications(AllOutcomes())
    shown = fixture.notifications(identity.outcome_viewer(fixture.live.registry))
    agent = fixture.notifications(OwnerOutcomes.of(fixture.source.provenance.require('sender')))
    key = (fixture.message.seq, fixture.message.message_id)
    before = {item.recipient_identity: item for item in raw.outcomes[key]}
    after = {item.recipient_identity: item for item in shown.outcomes[key]}
    assert before.keys() == after.keys()
    assert before and shown.read_marks == raw.read_marks
    assert agent.outcomes == raw.outcomes
    for recipient, item in before.items():
        visible = after[recipient]
        assert visible.state == item.state and visible.answer == item.answer
        assert visible.displayed_to == tuple(reader for reader in item.displayed_to
                                             if reader != identity.viewer)
    assert {reader.name for item in after.values() for reader in item.displayed_to} == (
        {'other'} if 'other' in painted else set())
    assert fixture.reads.read() == raw_document
    assert fixture.live.bus.reads.read() == live_document
    # The retained comparison is deliberately raw, not the projected zero/self subtraction.
    assert all(mark.displayed_to == fixture.reads.historical_displayed_recipient(
        mark.source.message, mark.recipient, fixture.source.provenance, document=raw_document,
    ) for mark in shown.read_marks)


def test_archive_display_requires_its_own_bus_even_for_the_same_viewer(archived_readers: ArchivedReaders) -> None:
    fixture = archived_readers
    fixture.paint('self')
    captured = fixture.selection('self').notification_identity
    live_page = fixture.live_page('self')
    assert not live_page.messages
    foreign = live_page.selection.notification_identity
    assert foreign.viewer == captured.viewer
    assert not foreign.bus_identity.matches(captured.bus_identity)
    raw = fixture.notifications(AllOutcomes())
    assert any(mark.displayed_to for mark in raw.read_marks)
    assert fixture.notifications(foreign.outcome_viewer(fixture.live.registry)).outcomes == raw.outcomes


def test_archive_viewer_rename_and_recreation_use_incarnation_not_current_name(archived_readers: ArchivedReaders) -> None:
    fixture = archived_readers
    fixture.paint('self')
    captured = fixture.selection('self').notification_identity
    before = fixture.notifications(captured.outcome_viewer(fixture.live.registry))
    raw = fixture.reads.read()
    fixture.live.registry.rename('self', 'renamed')
    renamed = fixture.selection('renamed').notification_identity
    assert renamed.viewer == captured.viewer
    assert fixture.notifications(renamed.outcome_viewer(fixture.live.registry)).outcomes == before.outcomes
    previous = fixture.live.registry.require('renamed')
    fixture.live.registry.unregister('renamed')
    fixture.live.registry.remove('renamed')
    fixture.live.registry.declare(Thread(
        'self', previous.tags, previous.worktree, created_at=111.0, role=UserThreadRole,
    ))
    recreated = fixture.selection('self').notification_identity
    assert recreated.viewer != captured.viewer
    other = fixture.notifications(recreated.outcome_viewer(fixture.live.registry))
    assert other.outcomes == fixture.notifications(AllOutcomes()).outcomes
    assert any(mark.displayed_to for mark in other.read_marks)
    assert fixture.reads.read() == raw


def test_direct_human_self_row_disappears_without_read_or_unread_mutation(comms: Comms, tmp_path: Path) -> None:
    worktree = Checkout(tmp_path)
    human = comms.messaging.user_identity(worktree)
    comms.registry.declare(Thread('sender', frozenset(), worktree))
    message = comms.messaging.send('sender', human.name, 'painted direct human message').message
    page = comms.views.dm_display_page(ThreadTarget('sender'), worktree=worktree)
    assert message in page.messages
    comms.views.mark_page_read(page, worktree=worktree)
    identity = page.selection.notification_identity
    document = comms.bus.reads.read()
    unread = comms.views.viewer_snapshot(worktree).unread
    full = comms.views.message_notifications_for_references(AllOutcomes(), (message.reference,))
    shown = comms.views.message_notifications_for_references(identity.outcome_viewer(comms.registry), (message.reference,))
    key = (message.seq, message.message_id)
    assert full.outcomes[key] and full.outcomes[key][0].displayed_to == (human.incarnation,)
    assert shown.outcomes[key] == ()
    assert MessageNotification.summary(shown.outcomes[key]) == DeliveryProgress.label
    assert shown.read_marks == full.read_marks
    assert not comms.bus.reads.changed_notifications(shown.read_marks, comms.registry.snapshot())
    assert comms.bus.reads.read() == document
    assert comms.views.viewer_snapshot(worktree).unread == unread
