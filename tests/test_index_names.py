"""Index declarations keep identity when a sibling is inserted before them."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.typed_table import Index, TypedStore, TypedTable
from cutover.carry_index_names import IndexNames


@dataclass(frozen=True)
class IndexNameScenarioRow(TypedTable):
    a: int
    b: int
    c: int
    indexes: ClassVar[tuple[Index, ...]] = (Index(("a",)), Index(("b",), unique=True))


@dataclass(frozen=True)
class IndexNameScenarioStore(TypedStore):
    root: Path
    filename = "index-name-scenario.sqlite3"
    rows = (IndexNameScenarioRow,)


def test_inserting_first_index_preserves_existing_names_and_opens_after_carry(tmp_path, monkeypatch):
    store = IndexNameScenarioStore(tmp_path)
    row = IndexNameScenarioRow(1, 2, 3)
    with store.writing() as db:
        row.insert(db)
    original = IndexNameScenarioRow.index_schema_objects()
    assert set(original) == {"index_name_scenario_a_idx", "index_name_scenario_unique_b_idx"}
    original_indexes = IndexNameScenarioRow.indexes
    monkeypatch.setattr(IndexNameScenarioRow, "indexes", (Index(("a", "b")), *original_indexes))
    expanded = IndexNameScenarioRow.index_schema_objects()
    assert set(original) < set(expanded)
    assert all(expanded[name] == sql for name, sql in original.items())
    with pytest.raises(RelationViolationError, match="differs from its declaration"):
        with store.snapshot():
            pytest.fail("A missing new index must still require the declared carry")
    assert IndexNames.carry(tmp_path, apply=False).changed == 1
    assert IndexNames.carry(tmp_path, apply=True).changed == 1
    with store.snapshot() as db:
        assert IndexNameScenarioRow.select(db) == [row]
        assert set(original) <= {item.name for item in store._schema(db)}
    assert IndexNames.carry(tmp_path, apply=True).changed == 0
    positive = Index(("a",), where="a > 0")
    negative = Index(("a",), where="a < 0")
    assert positive.name(IndexNameScenarioRow) != negative.name(IndexNameScenarioRow)
    oversized = Index(("a",), where=" OR ".join(["a > 0"] * 100))
    assert len(oversized.name(IndexNameScenarioRow)) == 255
    other = Index(("a",), where=" OR ".join(["a < 0"] * 100))
    assert oversized.name(IndexNameScenarioRow) != other.name(IndexNameScenarioRow)
