"""Current namespace seeks, including ordinary birth-zero reads and cutover."""

import json
import sqlite3
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.bus_route_counts import BusRouteCounts, OpenedRouteCounts
from agent_comms.comms import wire
from agent_comms.routing import DeliveryScope
from agent_comms.threads import Thread
from agent_comms.wire_record import PublicWireRecord


@pytest.mark.parametrize('rebound', [False, True])
def test_discovery_ignores_retired_source_history(tmp_path, rebound):
    c = wire(tmp_path)
    c.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    original = c.messaging.send_message('sender', viewer, 'current source')

    def measured():
        with c.bus.snapshot(viewer=viewer) as (basis, source):
            scope = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
            with OpenedRouteCounts(
                c.bus.log.path, source, basis.registry, {viewer: scope}
            ) as index:
                steps = []
                index.connection.set_progress_handler(lambda: steps.append(1) and 0, 1)
                try:
                    assert index.unseen_counts({viewer: basis.seen}) == {viewer: {'sender': 1}}
                    listed = [r.seq for r in index.unseen_deliveries(viewer, basis.seen)]
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
            c.registry.declare(Thread(name, frozenset(), str(tmp_path)))
            published = c.messaging.send_message(name, viewer, 'current source')
            if rebound:
                original = published
            else:
                c.registry.unregister(name)
                c.registry.remove(name)
        assert measured() <= initial + 32


def test_zero_birth_accepts_public_format_but_refuses_assigned_source(tmp_path):
    c = wire(tmp_path)
    c.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    original = c.messaging.send_message('sender', viewer, 'assigned source')
    with c.bus.snapshot(viewer=viewer) as (basis, source):
        threads = dict(basis.registry.threads)
        threads['sender'] = replace(threads['sender'], created_at=0.0)
        recorded = replace(basis.registry, threads=threads)
        scope = DeliveryScope.capture(viewer, recorded, basis.catalog)
        # The existing public-record owner supplies the unstamped saved format;
        # a current send always has an assigned stamp, even publish_ordinary.
        ordinary, = PublicWireRecord(original).delivery_messages()
        assert scope.unread(ordinary, recorded, basis.seen)
        with OpenedRouteCounts(c.bus.log.path, source, recorded, {viewer: scope}) as index:
            bindings = index._request_bindings((viewer,))
            assert json.loads(bindings['senders'])['sender'] == ['']
            assert 'sender' in json.loads(bindings['unrecorded'])
            with pytest.raises(sqlite3.OperationalError, match='user-defined function'):
                index.unseen_counts({viewer: basis.seen})
        # The read never grants the same participant identity-bound publication.
        with pytest.raises(ValueError, match='positive recorded birth'):
            scope.minimum_timestamp('sender', viewer, '1'*32, recorded)


def test_cutover_reuses_owned_rows_without_scanning_the_bus(tmp_path):
    c = wire(tmp_path)
    c.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    c.messaging.send_message('sender', viewer, 'prepared source')
    assert c.bus.pending_counts(viewer) == {'sender': 1}
    current = BusRouteCounts(c.bus.log.path).path
    old = current.with_name('bus_route_counts.v2.sqlite3')
    with sqlite3.connect(current) as before, sqlite3.connect(old) as copied:
        before.backup(copied)
    current.unlink()
    bus_before = c.bus.log.path.read_bytes()
    checkpoint_before = old.read_bytes()
    script = Path(__file__).parents[1] / 'tools/cutover/prepare_route_prefix_index.py'
    spec = spec_from_file_location('route_prefix_cutover', script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = module.prepare(old, c.bus.log.path)
    assert receipt['bytes_covered'] == len(bus_before)
    assert c.bus.log.path.read_bytes() == bus_before
    assert old.read_bytes() == checkpoint_before
    with patch.object(BusRouteCounts, '_decoded', side_effect=AssertionError('bus re-scan')):
        assert wire(tmp_path).bus.pending_counts(viewer) == {'sender': 1}
    with pytest.raises(FileExistsError, match='inspect, do not replay'):
        module.prepare(old, c.bus.log.path)
