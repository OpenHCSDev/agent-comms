"""The real carry folds per-claim cursor rows into each thread's newest and rekeys by thread."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from cutover.carry_native_cursor_thread_key import NativeCursorThreadKey

from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, RootId, ThreadLookup
from agent_comms.native_input_record import UnrecordedNativeColumn
from agent_comms.native_runtime_input import CurrentNativeCursor

THREAD_KEY = 'PRIMARY KEY ("wire_root_id", "recipient_lookup")'
CLAIM_KEY = 'PRIMARY KEY ("wire_root_id", "recipient_lookup", "owner_generation", "owner_claimed_generation")'


def cursor(lookup: str, claim: int, covered: int) -> CurrentNativeCursor:
    return CurrentNativeCursor(
        wire_root_id=FieldCodec.decode(RootId, "a" * 32), recipient_lookup=FieldCodec.decode(ThreadLookup, lookup * 32),
        owner_generation=1, owner_claimed_generation=claim, covered_seq=covered, injected_seq=0,
        input_id=UnrecordedNativeColumn(), stage=UnrecordedNativeColumn(),
        session_id=UnrecordedNativeColumn(), request_generation=UnrecordedNativeColumn(),
    )


def claim_keyed_store(root: Path, rows: list[CurrentNativeCursor]) -> Path:
    """The previous declaration: identical except that every claim keyed its own row."""
    root.mkdir(mode=0o700, parents=True)
    path = CoordinationStore.path_in(root)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(CurrentNativeCursor._table_sql().replace(THREAD_KEY, CLAIM_KEY))
        for row in rows:
            row.insert(db)
        for sql in CurrentNativeCursor.triggers().values():
            db.execute(sql)
    return path


def test_carry_keeps_each_threads_newest_cursor_and_rekeys_by_thread(tmp_path):
    root = tmp_path / "root"
    newest_b = cursor("b", 9, 30)
    newest_c = cursor("c", 4, 12)
    path = claim_keyed_store(root, [cursor("b", 2, 10), cursor("b", 5, 20), newest_b, newest_c])
    step = NativeCursorThreadKey(root, root, tmp_path / "spill")

    dry = step.carry(apply=False)
    assert (dry.changed, dry.detail, dry.rewritten) == (1, {"retired": 2}, ())
    with closing(sqlite3.connect(path)) as db:
        assert len(CurrentNativeCursor.select(db)) == 4

    assert step.carry(apply=True).rewritten == (path.relative_to(root),)
    with closing(sqlite3.connect(path)) as db:
        assert sorted(CurrentNativeCursor.select(db), key=lambda row: row.recipient_lookup) == [newest_b, newest_c]
        stored = {item.name: item.sql for item in CurrentNativeCursor.stored_objects(db)}
        assert stored[CurrentNativeCursor.declared_name] == CurrentNativeCursor._table_sql()
        assert set(CurrentNativeCursor.triggers()) <= set(stored)
        with pytest.raises(sqlite3.IntegrityError):
            replace(newest_b, owner_claimed_generation=10).insert(db)
    assert step.carry(apply=True).changed == 0


def test_carry_refuses_to_regress_a_thread_whose_older_claim_covered_more(tmp_path):
    root = tmp_path / "root"
    claim_keyed_store(root, [cursor("b", 2, 40), cursor("b", 5, 20)])

    with pytest.raises(RelationViolationError):
        NativeCursorThreadKey(root, root, tmp_path / "spill").carry(apply=False)
