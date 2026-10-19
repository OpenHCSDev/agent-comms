"""Current namespace seeks, including ordinary birth-zero reads."""

from agent_comms.read_basis import NamedViewer
import json
import sqlite3
from dataclasses import replace

import pytest

from agent_comms.bus_route_counts import OpenedRouteCounts
from agent_comms.comms import wire
from agent_comms.routing import DeliveryScope
from agent_comms.threads import Thread
from agent_comms.wire_record import PublicWireRecord


@pytest.mark.parametrize('rebound', [False, True])
def test_discovery_ignores_retired_source_history(tmp_path, rebound):
    c = wire(tmp_path)
    c.registry.declare(c.registry.new_thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    original = c.messaging.send('sender', viewer, 'current source').message

    def measured():
        with c.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
            scope = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
            with OpenedRouteCounts(
                c.bus.log.path, source, basis.registry, {viewer: scope}
            ) as index:
                steps = []
                index.connection.set_progress_handler(lambda: steps.append(1) and 0, 1)
                try:
                    assert index.unseen_counts({viewer: basis.seen(source)}) == {viewer: {'sender': 1}}
                    listed = [r.seq for r in index.unseen_deliveries(viewer, basis.seen(source))]
                    assert listed == [original.seq]
                finally:
                    index.connection.set_progress_handler(None, 0)
                return len(steps)

    initial = measured()
    for phase in range(2):
        for number in range(32):
            name = 'sender' if rebound else f'past-{phase}-{number}'
            if rebound:
                c.registry.unregister(name)
                c.registry.remove(name)
            c.registry.declare(c.registry.new_thread(name, frozenset(), str(tmp_path)))
            published = c.messaging.send(name, viewer, 'current source').message
            if rebound:
                original = published
            else:
                c.registry.unregister(name)
                c.registry.remove(name)
        assert measured() <= initial + 32


def test_zero_birth_accepts_public_format_but_refuses_assigned_source(tmp_path):
    c = wire(tmp_path)
    c.registry.declare(c.registry.new_thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    original = c.messaging.send('sender', viewer, 'assigned source').message
    with c.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        from agent_comms.registry_provenance import RegistryProvenance

        recorded = RegistryProvenance.capture(basis.registry)
        threads = dict(recorded.threads)
        threads['sender'] = replace(threads['sender'], created_at=0.0)
        recorded = replace(recorded, threads=threads)
        scope = DeliveryScope.capture(viewer, recorded, basis.catalog)
        # The existing public-record owner supplies the unstamped saved format;
        # a current send always has an assigned stamp, even publish_ordinary.
        ordinary, = PublicWireRecord(original).delivery_messages()
        assert scope.unread(ordinary, recorded, basis.seen(source))
        with OpenedRouteCounts(c.bus.log.path, source, recorded, {viewer: scope}) as index:
            bindings = index._request_bindings((viewer,))
            assert json.loads(bindings['senders'])['sender'] == ['']
            assert 'sender' in json.loads(bindings['unrecorded'])
            with pytest.raises(sqlite3.OperationalError, match='user-defined function'):
                index.unseen_counts({viewer: basis.seen(source)})
        # The read never grants the same participant identity-bound publication.
        with pytest.raises(ValueError, match='positive recorded birth'):
            scope.minimum_timestamp('sender', viewer, '1'*32, recorded)
