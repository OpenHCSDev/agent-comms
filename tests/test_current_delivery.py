"""Retained history never becomes work for a different thread incarnation."""

import sqlite3
from collections import Counter

import pytest

from agent_comms.bus_route_counts import BusRouteCounts, RouteEntryRow
from agent_comms.comms import wire
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread


def consistent(comms, *names):
    bulk = comms.bus.pending_counts_all(names)
    for name in names:
        inbox = comms.bus.inbox(name)
        assert bulk[name] == comms.bus.pending_count(name) == len(inbox)
        scope = comms.bus._delivery_scope(name)
        assert comms.bus.pending_counts(name) == Counter(
            scope.conversation(row.sender, row.target) for row in inbox
        )


@pytest.mark.parametrize("rebound", ["sender", "recipient"])
def test_current_inbox_bulk_and_index_exclude_rebound_history(tmp_path, monkeypatch, rebound):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(Thread(name, frozenset({"team"}), str(tmp_path)))
    old = comms.messaging.send_message("sender", "recipient", "retained history")
    comms.messaging.send("sender", "#team", "old channel row")
    comms.registry.unregister(rebound)
    comms.registry.remove(rebound)
    comms.registry.declare(Thread(rebound, frozenset({"team"}), str(tmp_path)))
    new = comms.messaging.send_message("sender", "recipient", "current input")
    comms.messaging.send("sender", "#team", "current channel input")
    current = wire(tmp_path)
    consistent(current, "sender", "recipient")
    assert current.bus.pending_counts("recipient") == {"sender": 1, "#team": 1}
    assert [row.seq for row in current.bus.inbox("recipient", "sender")] == [new.seq]
    assert old in current.bus.log.full_history()
    assert [row.body for row in current.bus.dm_history("sender", "recipient")] == [
        "retained history",
        "current input",
    ]

    def unavailable(*_args):
        raise sqlite3.DatabaseError("index unavailable")

    monkeypatch.setattr(BusRouteCounts, "sync", unavailable)
    uncached = wire(tmp_path)
    consistent(uncached, "sender", "recipient")
    assert [row.seq for row in uncached.bus.inbox("recipient", "sender")] == [new.seq]


def test_timestamp_index_handles_unordered_times_and_sparse_seen_rows(tmp_path):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    birth = comms.registry.require("recipient").created_at
    messages = [
        comms.bus.publisher.publish_ordinary(
            Message("sender", "recipient", f"row {index}", MessageType.INFO, timestamp=timestamp)
        )
        for index, timestamp in enumerate((birth + 2, birth - 1, birth + 1, birth - 2))
    ]
    basis = comms.bus.reads.capture(
        "recipient", [messages[0], messages[1]], comms.registry.snapshot(), comms.bus.log.path
    )
    comms.bus.reads.mark_displayed("recipient", basis)
    consistent(comms, "sender", "recipient")
    assert [row.seq for row in comms.bus.inbox("recipient")] == [messages[2].seq]
    with BusRouteCounts(comms.bus.log.path) as index:
        plan = index.connection.execute(
            f'EXPLAIN QUERY PLAN SELECT seq FROM "{RouteEntryRow.declared_name}" '
            "WHERE target=? AND sender=? AND sender_lookup=? AND timestamp>=?",
            ("recipient", "sender", "source", birth),
        ).fetchall()
    assert any("COVERING INDEX" in row[-1] for row in plan), plan


@pytest.mark.refactor_guard
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

    with BusRouteCounts(tmp_path / "bus.jsonl") as index:
        ExtraRouteStatisticRow(7).insert(index.connection)
        index.connection.commit()
        assert ExtraRouteStatisticRow.select(index.connection) == [ExtraRouteStatisticRow(7)]
        assert index.sync(None, None, lambda _: None)
        assert ExtraRouteStatisticRow.select(index.connection) == []
