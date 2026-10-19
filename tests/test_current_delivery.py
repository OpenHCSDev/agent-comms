"""Retained history never becomes work for a different thread incarnation."""

from agent_comms.wire_log import WireLog

from agent_comms.bus_publication import AgentOrigin
from agent_comms.read_basis import NamedViewer
import os
import sqlite3
from collections import Counter
from unittest.mock import patch

import pytest

from agent_comms.bus_route_counts import (
    BusRouteCounts, OpenedRouteCounts, RouteEntryRow, RouteSourceRow,
    RouteIndexReady, UnrecordedRouteSource,
)
from agent_comms.comms import wire
from agent_comms.messages import Message, InfoKind
from agent_comms.threads import Thread
from agent_comms.routing import DeliveryScope
from agent_comms.read_basis import ReadCursor, ConversationCursor
from agent_comms.read_ledger import ReadLedger


def consistent(comms, *names):
    bulk = comms.bus.pending_counts_all(names)
    for name in names:
        inbox = comms.bus.inbox(name)
        assert bulk[name] == comms.bus.pending_count(name) == len(inbox)
        with comms.bus.snapshot(viewer=NamedViewer(name)) as (basis, source):
            scope = DeliveryScope.capture(name, basis.registry, basis.catalog)
            assert comms.bus.pending_counts_opened(source, basis) == Counter(
                scope.conversation(row.sender, row.target) for row in inbox
            )


def test_missing_projection_has_no_revision_and_one_current_row_query(tmp_path):
    comms = wire(tmp_path)
    with BusRouteCounts(comms.bus.log.path) as index:
        queries = []
        index.connection.set_trace_callback(queries.append)
        state = RouteSourceRow.current(index.connection)
        index.connection.set_trace_callback(None)
        assert state == UnrecordedRouteSource()
        assert state.through == 0
        assert len(queries) == 1
        with comms.bus.log.opened_wire_snapshot() as source:
            assert not state.matches_source(source)
            assert state.needs_rebuild(source)
            state.check_source(source, RouteIndexReady())


@pytest.mark.parametrize("rebound", ["sender", "recipient"])
def test_current_inbox_bulk_and_index_exclude_rebound_history(tmp_path, rebound):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    old = comms.messaging.send("sender", "recipient", "retained history").message
    comms.messaging.send("sender", "#team", "old channel row").message.message_id
    comms.registry.unregister(rebound)
    comms.registry.remove(rebound)
    comms.registry.declare(comms.registry.new_thread(rebound, frozenset({'team'}), str(tmp_path)))
    new = comms.messaging.send("sender", "recipient", "current input").message
    comms.messaging.send("sender", "#team", "current channel input").message.message_id
    current = wire(tmp_path)
    consistent(current, "sender", "recipient")
    with current.bus.snapshot(viewer=NamedViewer("recipient")) as (basis, source):
        assert current.bus.pending_counts_opened(source, basis) == {"sender": 1, "#team": 1}
    assert [row.seq for row in current.bus.inbox("recipient", "sender")] == [new.seq]
    assert old in list(current.bus.full_history_page().messages)
    assert [row.body for row in list(current.views.dm_history_page("sender", "recipient").messages)] == [
        "retained history",
        "current input",
    ]


def test_timestamp_index_handles_unordered_times_and_sparse_seen_rows(tmp_path):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    birth = comms.registry.require("recipient").created_at
    messages = [
        comms.bus.publisher.publish_ordinary(
            Message("sender", "recipient", f"row {index}", InfoKind, timestamp=timestamp), origin=AgentOrigin()
        )
        for index, timestamp in enumerate((birth + 2, birth - 1, birth + 1, birth - 2))
    ]
    document = comms.bus.reads.read()
    with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
        basis = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
            'recipient', [messages[0], messages[1]], comms.registry.snapshot(), opened, index, document=document))
    comms.bus.reads.mark_displayed("recipient", basis)
    consistent(comms, "sender", "recipient")
    assert [row.seq for row in comms.bus.inbox("recipient")] == [messages[2].seq]
    with BusRouteCounts(comms.bus.log.path) as index:
        plan = index.connection.execute(
            f'EXPLAIN QUERY PLAN SELECT time_root FROM "{RouteEntryRow.declared_name}" '
            "WHERE target=? AND sender=? AND sender_lookup=? AND seq<=? "
            "ORDER BY seq DESC LIMIT 1",
            ("recipient", "sender", "source", messages[-1].seq),
        ).fetchall()
    assert any("USING INDEX" in row[-1] and "seq<?" in row[-1] for row in plan), plan


@pytest.mark.refactor_guard
def test_index_recorded_ahead_of_the_read_cut_answers_without_decoding(tmp_path):
    """A reader whose opened cut is behind the recorded index never rebuilds.

    While this reader holds its cut, another reader can index a later cut of
    the same file. Counts and unread listings stay inside the older cut,
    without decoding or regressing the recorded projection.
    """
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for number in range(5):
        comms.messaging.send("sender", viewer, f"row {number}").message
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
        assert comms.bus.pending_counts_opened(pending_source, pending_basis) == {"sender": 5}

    reader = wire(tmp_path)
    with reader.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, stale):
        comms.messaging.send("sender", viewer, "after the reader's cut").message
        # Another reader indexes a later cut while this cut is held.
        observer = wire(tmp_path)
        with observer.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
            assert observer.bus.pending_counts_opened(pending_source, pending_basis) == {"sender": 6}
        with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
            assert reader.bus.pending_counts_opened(stale, basis) == {"sender": 5}
            delivery = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
            with OpenedRouteCounts(
                reader.bus.log.path, stale, basis.registry, {viewer: delivery}
            ) as index:
                assert len(index.unseen_deliveries(viewer, basis.seen(stale))) == 5
            assert decode.call_count == 0
    with BusRouteCounts(comms.bus.log.path) as index:
        recorded = RouteSourceRow.current(index.connection)
    assert recorded.through > stale.revision.size


def test_truncated_source_fails_the_recorded_digest_and_rebuilds(tmp_path):
    """A source truncated in place below the recorded offset cannot answer.

    The append-only writer (wire_log.append_private_unlocked, os.O_APPEND)
    never truncates, so a digest-verified recorded revision is safe to answer
    from. A source that loses the bytes that digest covers fails it and
    rebuilds, the same way a replaced source does.
    """
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for number in range(5):
        comms.messaging.send("sender", viewer, f"row {number}").message
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
        assert comms.bus.pending_counts_opened(pending_source, pending_basis) == {"sender": 5}

    reader = wire(tmp_path)
    with reader.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, stale):
        comms.messaging.send("sender", viewer, "after the reader's cut").message
        comms.messaging.send("sender", viewer, "one more after it").message
        observer = wire(tmp_path)
        with observer.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
            assert observer.bus.pending_counts_opened(pending_source, pending_basis) == {"sender": 7}
        path = comms.bus.log.path
        with open(path, "rb") as scan:
            scan.seek(stale.revision.size)
            boundary = stale.revision.size + len(scan.readline())
        with BusRouteCounts(path) as index:
            recorded_offset = RouteSourceRow.current(index.connection).through
        assert stale.revision.size < boundary < recorded_offset
        os.truncate(path, boundary)
        with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
            assert reader.bus.pending_counts_opened(stale, basis) == {"sender": 5}
            assert decode.call_count > 0
    with BusRouteCounts(comms.bus.log.path) as index:
        recorded = RouteSourceRow.current(index.connection)
    assert recorded.through == stale.revision.size


def test_route_storage_and_decoder_have_one_declaration_owner():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[1] / "src/agent_comms"
    source = (root / "bus_route_counts.py").read_text()
    assert "CREATE TABLE" not in source and "CREATE INDEX" not in source
    assert "_pending_route_fields" not in (root / "message_bus.py").read_text()
    for declaration in ast.walk(ast.parse(source)):
        if isinstance(declaration, ast.ClassDef) and declaration.name.endswith("Row"):
            assert any(
                isinstance(base, ast.Name) and base.id in {"TypedTable", "TypedRow"}
                for base in declaration.bases
            )


def test_new_route_table_is_created_and_reset_with_its_scope(tmp_path):
    from dataclasses import dataclass
    from agent_comms.bus_route_counts import RouteTable
    from agent_comms.typed_table import TypedTable

    @dataclass(frozen=True)
    class ExtraRouteStatisticRow(RouteTable, TypedTable):
        observed: int

    from contextlib import closing

    comms = wire(tmp_path)
    routes = BusRouteCounts(comms.bus.log.path)
    routes.prepare()
    with closing(sqlite3.connect(routes.path)) as writer:
        ExtraRouteStatisticRow(7).insert(writer)
        writer.commit()
    with routes as index:
        assert ExtraRouteStatisticRow.select(index.connection) == [ExtraRouteStatisticRow(7)]
    with comms.bus.log.opened_wire_snapshot() as source:
        assert routes.sync(source, lambda _: ()).complete
    with routes as index:
        assert ExtraRouteStatisticRow.select(index.connection) == []


@pytest.mark.parametrize("birth_crossing", [False, True])
def test_retained_cut_clips_future_paint_in_counts_and_listings(tmp_path, birth_crossing):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    birth = comms.registry.require(viewer).created_at
    first = comms.bus.publisher.publish_ordinary(
        Message("sender", viewer, "inside cut", InfoKind, timestamp=birth + 2), origin=AgentOrigin()
    )
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        later = comms.bus.publisher.publish_ordinary(Message(
            "sender", viewer, "outside cut", InfoKind,
            timestamp=birth - 1 if birth_crossing else birth + 3,
        ), origin=AgentOrigin())
        document = comms.bus.reads.read()
        with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
            displayed = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
                viewer, (later,), basis.registry, opened, index, document=document))
        comms.bus.reads.mark_displayed(viewer, displayed)
        with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
            assert comms.bus.pending_counts_opened(pending_source, pending_basis) == {"sender": 1}
        later_reads = comms.bus.reads.read().cursor(
            viewer, basis.registry, source
        )
        assert later_reads == ReadCursor({
            ReadLedger.conversation(later, basis.registry): ConversationCursor().add((later.seq,))
        })
        delivery = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
        with OpenedRouteCounts(
            comms.bus.log.path, source, basis.registry, {viewer: delivery}
        ) as index:
            assert index.unseen_counts({viewer: later_reads}) == {viewer: {"sender": 1}}
            assert [row.seq for row in index.unseen_deliveries(viewer, later_reads)] == [first.seq]


def test_retained_source_uses_its_ledger_identity_after_path_replacement(tmp_path):
    from shutil import copyfile

    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send("sender", viewer, "already read").message
    document = comms.bus.reads.read()
    with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
        displayed = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
            viewer, (message,), comms.registry.snapshot(), opened, index, document=document))
    comms.bus.reads.mark_displayed(viewer, displayed)
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
        assert comms.bus.pending_counts_opened(pending_source, pending_basis) == {}
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        replacement = tmp_path / "replacement.jsonl"
        copyfile(comms.bus.log.path, replacement)
        replacement.chmod(0o600)
        replacement.replace(comms.bus.log.path)
        assert comms.bus._pending_projection(source, basis, (viewer,)) == {viewer: {}}
        assert comms.bus.pending_counts_opened(source, basis) == {}
