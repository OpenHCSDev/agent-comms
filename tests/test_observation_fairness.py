"""An unfinished observation cannot keep a real sidebar from being answered."""

from dataclasses import replace
from itertools import count
from multiprocessing import Pipe
from pathlib import Path

import pytest

from agent_comms.messaging.revision_views import StatusView
from agent_comms.presentation import WireRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.storage.private_path import FileRevision
from agent_comms.ui_model.observation import (
    NotificationReading,
    ObservationPending,
    ObservationService,
    ObserveNotifications,
    ObserveSidebar,
    ObserveTeamHud,
    ObserveThreads,
    ReadingObservation,
    SidebarReading,
    TeamHudReading,
    ThreadReading,
)


class EndlessReading(ReadingObservation):
    view = StatusView()

    def __init__(self, request):
        super().__init__(request)
        self.visits = 0
        self.finished = False

    def read(self, comms, revision):
        try:
            for _ in count():
                self.visits += 1
                yield ObservationPending()
        finally:
            self.finished = True


async def test_observation_pass_advances_each_interest_once(comms):
    client, observer = Pipe()
    with client, observer:
        service = ObservationService(comms, observer)
        endless = EndlessReading(ObserveThreads(frozenset()))
        sidebar = SidebarReading(ObserveSidebar(Checkout(comms.root.parent), True, True))
        service.interest.observations = {EndlessReading: endless, SidebarReading: sidebar}
        revision = comms.views.revision()
        try:
            for expected in range(1, 4):
                await service.interest.observe(service, revision, set())
                assert endless.visits == expected
            assert client.poll()
            assert client.recv().request == sidebar.request
            service.interest.remove(EndlessReading)
            assert endless.finished
        finally:
            service.retire()
            comms.views.transcript_reads.close()


class CountedThreadReading(ThreadReading):
    def __init__(self, request):
        super().__init__(request)
        self.read_count = 0

    def read(self, comms, revision):
        self.read_count += 1
        yield from super().read(comms, revision)


async def test_completed_thread_read_waits_for_changed_revision(comms):
    client, observer = Pipe()
    with client, observer:
        service = ObservationService(comms, observer)
        reading = CountedThreadReading(ObserveThreads(frozenset()))
        service.interest.observations = {CountedThreadReading: reading}
        try:
            for _ in range(3):
                await service.observe(set())
                assert reading.read_count == 1
                assert not service.changed.is_set()
            comms.channels.create_tag("revision-changed")
            for _ in range(3):
                await service.observe(set())
                assert reading.read_count == 2
                assert not service.changed.is_set()
        finally:
            service.retire()
            comms.views.transcript_reads.close()


class CountReads:
    def after_read(self, service):
        """This family test counts revision-driven reads, not asynchronous preparation."""

    def read(self, comms, revision):
        self.visits += 1
        yield ObservationPending()


@pytest.mark.parametrize('member,interest,own_field,unrelated_field', [
    (SidebarReading, ObserveSidebar(Checkout(Path('worktree')), True, True), 'bus_seq', 'team_frames'),
    (TeamHudReading, ObserveTeamHud(), 'team_frames', 'external_transcripts'),
    (NotificationReading, ObserveNotifications(frozenset()), 'reads', 'team_frames'),
    (ThreadReading, ObserveThreads(frozenset()), 'bus_seq', 'team_frames'),
])
async def test_reading_family_only_advances_for_its_declared_view(
    comms, tmp_path, member, interest, own_field, unrelated_field,
):
    marker = tmp_path / 'changed-source'
    marker.write_text('changed')
    file_revision = FileRevision.capture(marker).revisions()
    changes = {'bus_seq': 1, 'team_frames': file_revision, 'reads': file_revision,
               'external_transcripts': file_revision}
    counted = type(f'Counted{member.__name__}', (CountReads, member), {})
    reading = counted(interest)
    reading.visits = 0
    client, observer = Pipe()
    with client, observer:
        service = ObservationService(comms, observer)
        original = WireRevision.empty()
        try:
            for _ in range(3):
                reading.step(service, original)
            assert reading.visits == 1
            unrelated = replace(original, **{unrelated_field: changes[unrelated_field]})
            for _ in range(3):
                reading.step(service, unrelated)
            assert reading.visits == 1
            own = replace(unrelated, **{own_field: changes[own_field]})
            for _ in range(3):
                reading.step(service, own)
            assert reading.visits == 2
        finally:
            reading.close()
            service.retire()
            comms.views.transcript_reads.close()


def test_reading_without_a_declared_view_fails_at_class_creation():
    with pytest.raises(TypeError, match='must declare its revision view'):
        class UndeclaredReading(ReadingObservation):
            def read(self, comms, revision):
                yield ObservationPending()
