"""One-time cache retirement preserves source rows and refuses unknown ownership."""

import sqlite3
from pathlib import Path

import pytest

from agent_comms.bus import Bus, PrivateRecord
from agent_comms.bus_tables import BusSchema
from agent_comms.cohort_schema import AwarenessTable
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried
from agent_comms.historical_views import HistoryArchive
from agent_comms.maintenance_barrier import MaintenanceNotHeldError
from agent_comms.messages import InfoKind, Message
from agent_comms.private_runtime_schema import PrivateCoordinatorSchema, PrivateCoordinatorTable
from agent_comms.typed_table import SQLiteTableName, WriterOwnedFile
from bus_record_fixture import record_from
from conftest import maintenance_held
from test_historical_views import collect, migrated
from cutover.carry_reply_policy import ReplyPolicy, StoredDirectReply


def test_complete_file_reconcile_preserves_and_rejects_unclaimed_tables():
    schema = BusSchema
    with sqlite3.connect(":memory:") as db:
        schema.create_schema(db)
        assert not schema.schema_differs(db)
        db.execute("CREATE TABLE unknown_owner (payload TEXT)")
        db.execute("INSERT INTO unknown_owner VALUES ('retain this input')")
        db.commit()
        assert schema.schema_differs(db)
        with pytest.raises(schema.schema_error):
            schema.reconcile_schema(db, WriterOwnedFile())
        assert db.execute("SELECT payload FROM unknown_owner").fetchall() == [("retain this input",)]


def test_mandatory_private_view_preserves_optional_awareness_ownership(tmp_path):
    with Coordination(str(tmp_path / "private.sqlite3")) as store:
        PrivateCoordinatorTable.install(store)
        db = store.session._connection
        assert AwarenessTable.stored_schema(db)
        PrivateCoordinatorSchema.require_schema(db)
        optional, = AwarenessTable.schema_rows()
        with store.session.writing() as writer:
            SQLiteTableName(optional.declared_name).drop(writer)
        assert AwarenessTable.schema_differs(db)
        PrivateCoordinatorSchema.require_schema(db)


def test_retired_reply_policy_carry_is_dry_idempotent_and_source_preserving(bus, tmp_path):
    reply = bus.append(lambda seq, db: record_from(
        PrivateRecord, Message("author", "worker", "Retain the actual delivery", InfoKind, seq=seq),
    ))
    with bus.writing() as db:
        StoredDirectReply.create(db)
        StoredDirectReply("b" * 32, reply.seq).insert(db)
        db.execute("CREATE TABLE unknown_owner (payload TEXT)")
        db.execute("INSERT INTO unknown_owner VALUES ('unclassified')")
    step = ReplyPolicy(bus.path.parent, bus.path.parent, tmp_path / "spill")
    assert step.backup_files() == (bus.path,)
    pending = step.carry(apply=False)
    assert pending == Carried(1, {"retired_direct_replies": 1}, (bus.path.name,))
    with bus.observing() as db:
        assert StoredDirectReply.count(db) == 1
    with pytest.raises(MaintenanceNotHeldError):
        step.carry(apply=True)
    with maintenance_held(bus.path.parent):
        with pytest.raises(RelationViolationError):
            step.carry(apply=True)
    with bus.observing() as db:
        assert StoredDirectReply.present(db)
        value, = db.execute("SELECT payload FROM unknown_owner").fetchone()
        assert value == "unclassified"
        with pytest.raises(RelationViolationError):
            BusSchema.require_schema(db)
    assert bus.delivery(reply.seq) == reply
    with bus.writing() as db:
        SQLiteTableName("unknown_owner").drop(db)
    with maintenance_held(bus.path.parent):
        assert step.carry(apply=True) == pending
        assert step.carry(apply=True) == Carried(0)
    with bus.observing() as db:
        assert not StoredDirectReply.present(db)
        BusSchema.require_schema(db)


def test_retired_reply_policy_carry_rejects_an_unrecognized_input_shape(bus, tmp_path):
    with bus.writing() as db:
        StoredDirectReply.create(db)
        db.execute('ALTER TABLE "direct_reply_source" ADD COLUMN unrelated TEXT')
    step = ReplyPolicy(bus.path.parent, bus.path.parent, tmp_path / "spill")
    for apply in (False, True):
        with pytest.raises(RelationViolationError):
            step.carry(apply=apply)
    with bus.observing() as db:
        assert StoredDirectReply.present(db)


def test_retired_reply_policy_carries_catalog_snapshots_without_resealing(migrated, tmp_path):
    _, _, live, snapshot = migrated
    archived = Bus(Bus.path_in(Path(snapshot.root)))
    selected = (live.bus.bus, archived)
    for source in selected:
        original = source.delivery(source.revision())
        with source.writing() as db:
            StoredDirectReply.create(db)
            StoredDirectReply(original.audience.sender_lookup, original.seq).insert(db)
            db.execute('ALTER TABLE "message_identity_source" ADD COLUMN response_policy TEXT')
    catalog = HistoryArchive.path_in(live.root)
    before_catalog = catalog.read_bytes()
    viewer = live.views.display_viewer(str(live.root))
    before_rows = collect(live, "channel_display_page", "#team", viewer=viewer)
    step = ReplyPolicy(live.root, live.root, tmp_path / "spill")
    assert {source.path for source in selected} <= set(step.backup_files())
    pending = step.carry(apply=False)
    assert pending.changed == 2
    assert set(pending.rewritten) == {str(source.path.relative_to(live.root)) for source in selected}
    with maintenance_held(live.root):
        assert step.carry(apply=True) == pending
    for source in selected:
        with source.observing() as db:
            assert not StoredDirectReply.present(db)
            BusSchema.require_schema(db)
    snapshot.validate()
    assert catalog.read_bytes() == before_catalog
    assert collect(live, "channel_display_page", "#team", viewer=viewer) == before_rows
