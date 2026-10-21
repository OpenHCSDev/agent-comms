import sqlite3
from contextlib import closing
from dataclasses import fields, replace

import pytest

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import Bus, BusRecord, KeyedPublicRecord, PrivateRecord, PublicRecord
from agent_comms.bus_publication import Delivered
from agent_comms.bus_tables import BusTables, KeyedPublication
from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement, canonical_publication_key, public_envelope_digest,
)
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.typed_table import TypedTable
from agent_comms.wake import ControlClassification, resolve_wake_cohort


def record_from(declaration, message):
    values = {item.name: getattr(message, item.name) for item in fields(Message)}
    if issubclass(declaration, Delivered):
        audience = freeze_audience(
            message, (FrozenRecipient("b" * 32, message.target),), "c" * 64,
            sender_lookup="a" * 32, sender_name=message.sender)
        values.update(audience=audience, decisions=resolve_wake_cohort(
            message, frozen_audience=audience, control=ControlClassification.ORDINARY),
            control=ControlClassification.ORDINARY.value)
    if issubclass(declaration, KeyedPublication):
        values["acknowledgement"] = KeyedResponseAcknowledgement(
            "d" * 32, "e" * 32, canonical_publication_key("e" * 32, message.target),
            public_envelope_digest(message.to_wire()))
    return declaration(**values)


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
def test_real_message_record_round_trip(tmp_path, declaration):
    bus = Bus(tmp_path / "bus.sqlite3")
    message = Message("author", "worker", "hello", InfoKind, seq=1)
    record = record_from(declaration, message)
    seq = bus.append(record)
    assert seq > 0
    assert bus.lookup(seq) == replace(record, seq=seq)
    assert Bus(bus.path).lookup(seq).seq == seq
    assert bus.page(seq + 1, 10) == (replace(record, seq=seq),)
    with pytest.raises(RelationViolationError):
        bus.lookup(seq + 1)


def test_acknowledgement_is_idempotent_without_extra_rows(tmp_path):
    path = tmp_path / "bus.sqlite3"
    bus = Bus(path)
    record = record_from(KeyedPublicRecord, Message("author", "worker", "first", InfoKind))
    first = bus.append(record)
    with closing(sqlite3.connect(path)) as db:
        before = {table: table.count(db) for table in TypedTable.members_with(BusTables)}
    assert bus.append(replace(record, body="different")) == first
    with closing(sqlite3.connect(path)) as db:
        assert {table: table.count(db) for table in TypedTable.members_with(BusTables)} == before
        assert db.execute("SELECT seq FROM sqlite_sequence").fetchall() == [(first,)]
    assert bus.lookup(first) == replace(record, seq=first)


def test_every_bus_table_rejects_update_and_delete(tmp_path):
    path = tmp_path / "bus.sqlite3"
    bus = Bus(path)
    declaration = next(member for member in BusRecord.members_with(BusRecord)
                       if issubclass(member, Delivered) and issubclass(member, KeyedPublication))
    record = record_from(declaration, Message("author", "worker", "kept", InfoKind, seq=1))
    seq = bus.append(record)
    with closing(sqlite3.connect(path)) as db:
        for table in TypedTable.members_with(BusTables):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f'UPDATE "{table.declared_name}" SET seq=seq')
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f'DELETE FROM "{table.declared_name}"')
    assert bus.lookup(seq) == replace(record, seq=seq)


def test_inbox_filters_target_and_orders_by_sequence(tmp_path):
    bus = Bus(tmp_path / "bus.sqlite3")
    first = record_from(PublicRecord, Message("author", "worker", "one", InfoKind))
    other = record_from(PublicRecord, Message("author", "other", "two", InfoKind))
    last = record_from(PrivateRecord, Message("author", "worker", "three", InfoKind, seq=3))
    first_seq = bus.append(first)
    bus.append(other)
    last_seq = bus.append(last)
    stored_first, stored_last = replace(first, seq=first_seq), replace(last, seq=last_seq)
    assert first_seq < last_seq
    assert bus.inbox("worker", 0, 10) == (stored_first, stored_last)
    assert bus.inbox("worker", first_seq, 10) == (stored_last,)
    assert bus.inbox("worker", 0, 1) == (stored_first,)
    assert bus.inbox("other", 0, 10) == (replace(other, seq=first_seq + 1),)
    assert bus.inbox("absent", 0, 10) == ()
    assert bus.page(last_seq + 1, 2) == (stored_last, replace(other, seq=first_seq + 1))
