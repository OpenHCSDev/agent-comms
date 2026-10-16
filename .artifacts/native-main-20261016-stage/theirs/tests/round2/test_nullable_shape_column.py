"""Physical SQL NULL decodes to mandatory state through the original codec."""

import sqlite3
from dataclasses import dataclass, field

import pytest

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import JsonShapeFamily, JsonShapeMember
from agent_comms.typed_table import Column, IntegerStorage, TypedTable


class StoredEpoch(DeclaredFamily, JsonShapeFamily, affix="Epoch"):
    pass


@dataclass(frozen=True)
class AbsentEpoch(JsonShapeMember, StoredEpoch):
    value: None = None


@dataclass(frozen=True)
class RecordedEpoch(JsonShapeMember, StoredEpoch):
    value: int


@dataclass(frozen=True)
class NullableShapeRow(TypedTable):
    key: str = field(metadata={"sql": Column(primary_key=True)})
    epoch: StoredEpoch = field(
        metadata={"sql": Column(storage=IntegerStorage, nullable=True, check="epoch>0")}
    )
    optional_count: int | None = None


@dataclass(frozen=True)
class RequiredShapeRow(TypedTable):
    key: str = field(metadata={"sql": Column(primary_key=True)})
    epoch: StoredEpoch = field(metadata={"sql": Column(storage=IntegerStorage)})


def test_sql_nullable_shape_roundtrips_without_optional_semantic_value(tmp_path):
    with sqlite3.connect(tmp_path / "nominal-null.sqlite3") as db:
        NullableShapeRow.create(db)
        RequiredShapeRow.create(db)
        declared = {column[1]: column for column in db.execute("PRAGMA table_info(nullable_shape)")}
        assert declared["epoch"][2:4] == ("INTEGER", 0)
        assert declared["optional_count"][3] == 0
        assert declared["key"][3] == 1
        absent = NullableShapeRow("absent", AbsentEpoch())
        recorded = NullableShapeRow("recorded", RecordedEpoch(7))
        absent.insert(db)
        recorded.insert(db)
        assert db.execute("SELECT key,epoch,typeof(epoch) FROM nullable_shape ORDER BY key").fetchall() == [
            ("absent", None, "null"), ("recorded", 7, "integer")
        ]
        assert NullableShapeRow.select(db) == [absent, recorded]
        NullableShapeRow.update(db, where="key=?", parameters=("absent",), epoch=RecordedEpoch(9))
        assert NullableShapeRow.one(db, key="absent").epoch == RecordedEpoch(9)
        NullableShapeRow.update(db, where="key=?", parameters=("absent",), epoch=AbsentEpoch())
        assert NullableShapeRow.one(db, key="absent") == absent
        with pytest.raises(sqlite3.IntegrityError):
            RequiredShapeRow("no-null", AbsentEpoch()).insert(db)
        with pytest.raises(sqlite3.IntegrityError):
            NullableShapeRow("zero", RecordedEpoch(0)).insert(db)
        for invalid in (True, "7", 1.5):
            with pytest.raises(ValueError):
                NullableShapeRow("wrong-shape", RecordedEpoch(invalid)).insert(db)
        db.execute("INSERT INTO nullable_shape(key,epoch) VALUES('externally-written',NULL)")
        assert NullableShapeRow.one(db, key="externally-written").epoch == AbsentEpoch()
