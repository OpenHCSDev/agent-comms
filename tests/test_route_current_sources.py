"""Current namespace seeks, including ordinary birth-zero reads."""

import sqlite3
from dataclasses import replace

import pytest

from agent_comms.bus_route_counts import OpenedRouteCounts, RouteSenderBindingRow
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


def test_unstamped_and_zero_birth_sources_cannot_match_current_delivery(tmp_path):
    c = wire(tmp_path)
    c.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    original = c.messaging.send_message('sender', viewer, 'assigned source')
    with c.bus.snapshot(viewer=viewer) as (basis, source):
        current = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
        ordinary, = PublicWireRecord(original).delivery_messages()
        assert not current.unread(ordinary, basis.registry, basis.seen)
        threads = dict(basis.registry.threads)
        threads['sender'] = replace(threads['sender'], created_at=0.0)
        recorded = replace(basis.registry, threads=threads)
        scope = DeliveryScope.capture(viewer, recorded, basis.catalog)
        with pytest.raises(ValueError, match='creation identity is unavailable'):
            scope.unread(ordinary, recorded, basis.seen)
        with pytest.raises(ValueError, match='creation identity is unavailable'):
            with OpenedRouteCounts(c.bus.log.path, source, recorded, {viewer: scope}):
                pytest.fail('A namespace without creation identity cannot be admitted')
        with pytest.raises(ValueError, match='creation identity is unavailable'):
            scope.minimum_timestamp('sender', viewer, '1'*32, recorded)


def test_sender_namespace_is_bound_once_and_reader_stays_sealed(tmp_path, monkeypatch):
    c = wire(tmp_path)
    c.registry.declare(Thread('sender', frozenset(), str(tmp_path)))
    viewer = c.messaging.user_identity(str(tmp_path)).name
    message = c.messaging.send_message('sender', viewer, 'unpainted')
    with c.bus.snapshot(viewer=viewer) as (basis, source):
        scope = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
        original = type(basis.registry).named_members
        visits = []

        def named_members(registry):
            if registry is basis.registry:
                visits.append(1)
            yield from original(registry)

        monkeypatch.setattr(type(basis.registry), 'named_members', named_members)
        with OpenedRouteCounts(c.bus.log.path, source, basis.registry, {viewer: scope}) as index:
            assert index.unseen_counts({viewer: basis.seen}) == {viewer: {'sender': 1}}
            assert [row.seq for row in index.unseen_deliveries(viewer, basis.seen)] == [message.seq]
            assert index.unseen_counts({viewer: basis.seen}) == {viewer: {'sender': 1}}
            assert visits == [1]
            assert index.connection.execute('PRAGMA query_only').fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                index.connection.execute(f'DELETE FROM "{RouteSenderBindingRow.declared_name}"')
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                index.connection.execute('CREATE TABLE main.reader_cannot_write (value TEXT)')
