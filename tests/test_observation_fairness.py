"""An unfinished observation cannot keep a real sidebar from being answered."""

from itertools import count
from multiprocessing import Pipe

from agent_comms.ui_model.observation import (
    ObservationPending,
    ObservationService,
    ObserveSidebar,
    ObserveThreads,
    ReadingObservation,
    SidebarReading,
    ThreadReading,
)


class EndlessReading(ReadingObservation):
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
        sidebar = SidebarReading(ObserveSidebar(str(comms.root.parent), True, True))
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
