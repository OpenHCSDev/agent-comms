"""A retained original cut is independent of later routes and timestamp history."""

from unittest.mock import patch

from agent_comms.bus_route_counts import BusRouteCounts, OpenedRouteCounts, RouteSourceRow
from agent_comms.comms import wire
from agent_comms.messages import InfoKind, Message
from agent_comms.routing import DeliveryScope
from agent_comms.threads import Thread


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
    comms.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    with comms.bus.snapshot(viewer=viewer) as (basis, source):
        assert source.through == 0
        message = comms.messaging.send_message('sender', viewer, 'after empty capture')
        assert wire(tmp_path).bus.pending_counts(viewer) == {'sender': 1}
        with BusRouteCounts(comms.bus.log.path) as index:
            before = RouteSourceRow.select(index.connection)
        assert comms.bus.pending_counts_opened(source, basis) == {}
        with BusRouteCounts(comms.bus.log.path) as index:
            assert RouteSourceRow.select(index.connection) == before
        assert [row.seq for row in comms.bus.inbox(viewer)] == [message.seq]


def test_retained_rank_and_listing_do_not_visit_later_messages_or_routes(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    birth = comms.registry.require(viewer).created_at
    originals = [comms.bus.publisher.publish_ordinary(Message(
        'sender', viewer, f'original {number}', InfoKind, timestamp=timestamp,
    )) for number, timestamp in enumerate((birth+2, birth-1, birth+2, birth+1))]
    comms.bus.reads.mark_displayed(viewer, comms.bus.reads.capture(
        viewer, (originals[0],), comms.registry.snapshot(), comms.bus.log.path,
    ))
    with comms.bus.snapshot(viewer=viewer) as (basis, source):
        delivery = DeliveryScope.capture(viewer, basis.registry, basis.catalog)

        def retained():
            calls = []
            original = DeliveryScope.minimum_timestamp

            def since(self, *args):
                calls.append(args)
                return original(self, *args)

            with patch.object(DeliveryScope, 'minimum_timestamp', since):
                with OpenedRouteCounts(
                    comms.bus.log.path, source, basis.registry, {viewer: delivery},
                ) as index:
                    answer = measure(index, viewer, basis.seen)
            assert answer[:2] == (
                {viewer: {'sender': 2}}, (originals[2].seq, originals[3].seq),
            )
            return answer[2], len(calls)

        initial = retained()
        for phase in range(2):
            for number in range(256):
                timestamp = birth-1 if number % 3 == 0 else birth+2+number/1000
                comms.bus.publisher.publish_ordinary(Message(
                    'sender', viewer, f'later {phase}/{number}', InfoKind, timestamp=timestamp,
                ))
            for number in range(8):
                sender = f'later-{phase}-{number}'
                comms.registry.declare(Thread(sender, frozenset(), str(tmp_path)))
                comms.messaging.send_message(sender, viewer, 'a route absent at capture')
            wire(tmp_path).bus.pending_counts(viewer)  # Real independent catchup.
            after = retained()
            assert after[1] == initial[1]  # No future route eligibility calls.
            assert after[0] <= initial[0] + 32  # No suffix or timestamp-range scan.
