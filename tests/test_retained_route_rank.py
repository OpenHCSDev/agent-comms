"""A retained original cut is independent of later routes and timestamp history."""


from agent_comms.read_basis import NamedViewer

from agent_comms.bus_route_counts import BusRouteCounts, RouteSourceRow
from agent_comms.comms import wire


def measure(index, actor, seen):
    steps = 0

    def step():
        nonlocal steps
        steps += 1
        return 0

    index.connection.set_progress_handler(step, 1)
    try:
        counts = index.unseen_counts({actor: seen})
        listed = index.unseen_deliveries(actor, seen)
    finally:
        index.connection.set_progress_handler(None, 0)
    return counts, tuple(row.seq for row in listed), steps


def test_original_empty_cut_cannot_clear_a_later_index(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        assert source.through == 0
        message = comms.messaging.send('sender', viewer, 'after empty capture').message
        observer = wire(tmp_path)
        with observer.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
            assert observer.bus.pending_counts_opened(pending_source, pending_basis) == {'sender': 1}
        with BusRouteCounts(comms.bus.log.path) as index:
            before = RouteSourceRow.current(index.connection)
        assert comms.bus.pending_counts_opened(source, basis) == {}
        with BusRouteCounts(comms.bus.log.path) as index:
            assert RouteSourceRow.current(index.connection) == before
        assert [row.seq for row in comms.bus.inbox(viewer)] == [message.seq]


