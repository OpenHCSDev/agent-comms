import sqlite3
from contextlib import closing

import pytest

from agent_comms.bus import Bus, BusTables, PrivateDelivery, PublicMessage
from agent_comms.errors import RelationViolationError
from agent_comms.typed_table import TypedTable


def test_increasing_sequences_and_both_record_round_trips(tmp_path):
    path = tmp_path / "bus.sqlite3"
    bus = Bus(path)
    public = PublicMessage(sender="author", target="#room", publication_key="public", body="hello")
    private = PrivateDelivery(sender="author", target="worker", publication_key="private",
                              body="work", claim_owner="worker")
    first = bus.append(public)
    second = bus.append(private)
    assert 0 < first < second
    assert bus.lookup(first) == public
    assert bus.lookup(second) == private
    assert Bus(path).lookup(second) == private
    assert bus.page(second + 1, 10) == (private, public)
    assert bus.page(second, 1) == (public,)
    with pytest.raises(RelationViolationError):
        bus.lookup(second + 1)


def test_publication_key_is_idempotent_without_extra_rows(tmp_path):
    path = tmp_path / "bus.sqlite3"
    bus = Bus(path)
    original = PublicMessage(sender="author", target="#room", publication_key="same", body="first")
    first = bus.append(original)
    with closing(sqlite3.connect(path)) as db:
        before = {table: table.count(db) for table in TypedTable.members_with(BusTables)}
    duplicate = PrivateDelivery(sender="other", target="worker", publication_key="same",
                                body="different", claim_owner="worker")
    assert bus.append(duplicate) == first
    with closing(sqlite3.connect(path)) as db:
        assert {table: table.count(db) for table in TypedTable.members_with(BusTables)} == before
        assert db.execute("SELECT seq FROM sqlite_sequence").fetchall() == [(first,)]
    assert bus.lookup(first) == original


def test_every_bus_table_rejects_update_and_delete(tmp_path):
    path = tmp_path / "bus.sqlite3"
    bus = Bus(path)
    record = PrivateDelivery(sender="author", target="worker", publication_key="immutable",
                             body="kept", claim_owner="worker")
    seq = bus.append(record)
    with closing(sqlite3.connect(path)) as db:
        for table in TypedTable.members_with(BusTables):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f'UPDATE "{table.declared_name}" SET seq=seq')
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f'DELETE FROM "{table.declared_name}"')
    assert bus.lookup(seq) == record


def test_inbox_filters_target_and_orders_by_sequence(tmp_path):
    bus = Bus(tmp_path / "bus.sqlite3")
    first = PublicMessage(sender="a", target="worker", publication_key="one", body="one")
    other = PublicMessage(sender="b", target="other", publication_key="two", body="two")
    last = PrivateDelivery(sender="c", target="worker", publication_key="three", body="three",
                           claim_owner="worker")
    first_seq = bus.append(first)
    bus.append(other)
    bus.append(last)
    assert bus.inbox("worker", 0, 10) == (first, last)
    assert bus.inbox("worker", first_seq, 10) == (last,)
    assert bus.inbox("worker", 0, 1) == (first,)
    assert bus.inbox("other", 0, 10) == (other,)
    assert bus.inbox("absent", 0, 10) == ()
