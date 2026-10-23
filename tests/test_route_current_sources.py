"""Current delivery queries skip retired sender incarnations."""

from agent_comms.read_basis import NamedViewer
from agent_comms.page_traversal import OlderTraversal
import pytest

from agent_comms.routing import DeliveryScope
from agent_comms.threads import Thread


@pytest.mark.parametrize('rebound', [False, True])
def test_discovery_ignores_retired_source_history(tmp_path, rebound, comms):
    c = comms
    c.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    original = c.messaging.send('sender', viewer, 'current source').message

    def measured():
        with c.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
            scope = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
            with source.observing() as db:
                steps = []
                db.set_progress_handler(lambda: steps.append(1) and 0, 1)
                try:
                    assert source.unseen_counts(
                        basis.registry, {viewer: scope}, {viewer: basis.seen(source)},
                    ) == {viewer: {'sender': 1}}
                    listed = [r.seq for r in source.unseen_deliveries(
                        basis.registry, scope, basis.seen(source), traversal=OlderTraversal())]
                    assert listed == [original.seq]
                finally:
                    db.set_progress_handler(None, 0)
                return len(steps)

    initial = measured()
    for phase in range(2):
        for number in range(32):
            name = 'sender' if rebound else f'past-{phase}-{number}'
            if rebound:
                c.registry.unregister(name)
                c.registry.remove(name)
            c.registry.declare(Thread(name, frozenset(), str(tmp_path)))
            published = c.messaging.send(name, viewer, 'current source').message
            if rebound:
                original = published
            else:
                c.registry.unregister(name)
                c.registry.remove(name)
        assert measured() <= initial + 32
