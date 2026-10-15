"""The CLI reports real stored DDL; the carry changes only a new copy."""
import json
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from agent_comms.cli import main
from agent_comms.cli_commands import SchemaReportCliCommand
from agent_comms.schema_report import StoreSchemaReport
from agent_comms.todos import Todo
from agent_comms.typed_table import SQLiteSchemaObject, SQLiteSchemaDifference
from test_private_bus_checkpoint import _root
from tools.cutover.carry_declared_rules import carry


# The two stored rules as they were before the delivered state was declared.
OLDER_RULES = json.loads((Path(__file__).parent / "data" / "coordination_rules_before_delivered.json").read_text())


def _old_todo(path, transform=lambda sql: sql):
    """A store created before the selector CHECKs were declared."""
    target = Todo.schema_objects()[Todo.declared_name]
    original = target
    for item in Todo._fields():
        for constraint in item.storage.family_constraints(item.name, item.annotation):
            original = original.replace(f" CHECK ({constraint})", "", 1)
    original = transform(original)
    assert original != target
    with closing(sqlite3.connect(path)) as db:
        db.execute(original)
        db.execute("INSERT INTO todo (id,repo,text,creator,creator_created,state,revision) "
                   "VALUES ('one','team/repo','unfinished','Alice',17002,'open',1)")
        db.commit()


def test_comparison_names_changed_missing_and_extra_objects():
    assert SQLiteSchemaObject.differences(
        [SQLiteSchemaObject("changed", "old"), SQLiteSchemaObject("extra", "sql")],
        {"changed": "new", "missing": "sql"}) == (
            SQLiteSchemaDifference("changed", "old", "new"),
            SQLiteSchemaDifference("extra", "sql", None),
            SQLiteSchemaDifference("missing", None, "sql"))


def test_cli_inspects_existing_stores_without_rewriting_them(tmp_path, capsys, monkeypatch):
    # A disposable bus is not this agent's private owner-launch root.
    from agent_comms.private_nk_entrypoint import ROOT_ID_ENV, PACKAGE_ENV
    monkeypatch.delenv(ROOT_ID_ENV, raising=False)
    monkeypatch.delenv(PACKAGE_ENV, raising=False)
    comms, _ = _root(tmp_path)
    path = comms.root / "todos.sqlite3"
    _old_todo(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = SchemaReportCliCommand().apply(comms)
    todo = next(row for row in result if row["path"] == str(path))
    assert [change["name"] for change in todo["differences"]] == [Todo.declared_name]
    assert main(["--root", str(comms.root), "schema-report"]) == 0
    assert '"differences"' in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_unknown_and_unavailable_stores_do_not_look_clean(tmp_path):
    comms, _ = _root(tmp_path)
    with closing(sqlite3.connect(comms.root / "unknown.sqlite3")) as db:
        db.execute("CREATE TABLE unowned (x)")
    (comms.root / "broken.sqlite3").write_bytes(b"not a database")
    reports = SchemaReportCliCommand().apply(comms)
    assert next(row for row in reports if row["path"].endswith("unknown.sqlite3"))["undeclared_tables"] == ["unowned"]
    assert next(row for row in reports if row["path"].endswith("broken.sqlite3"))["error"]


def test_carry_preserves_rows_and_source_and_refuses_overwrite(tmp_path):
    source = tmp_path / "old.sqlite3"
    _old_todo(source)
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE todo SET rowid=37")
        db.commit()
    before = source.read_bytes()
    target = tmp_path / "carried.sqlite3"
    result = carry(source, target)
    assert result["carried"] == [Todo.declared_name]
    assert source.read_bytes() == before
    assert StoreSchemaReport.observe(target).differences == ()
    with closing(sqlite3.connect(target)) as db:
        assert db.execute("SELECT text FROM todo WHERE id='one'").fetchone() == ("unfinished",)
        assert db.execute("SELECT rowid FROM todo WHERE id='one'").fetchone() == (37,)
        # The carried rules are enforced, not only stored.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE todo SET state='invalid'")
    with pytest.raises(FileExistsError):
        carry(source, target)


def test_carry_refuses_rows_that_violate_the_current_rules(tmp_path):
    source = tmp_path / "old.sqlite3"
    _old_todo(source)
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE todo SET state='invalid'")
        db.commit()
    before = source.read_bytes()
    with pytest.raises(sqlite3.IntegrityError):
        carry(source, tmp_path / "invalid.sqlite3")
    assert source.read_bytes() == before
    assert not (tmp_path / "invalid.sqlite3").exists()


@pytest.mark.parametrize("transform", [
    lambda sql: sql.replace(") STRICT", ', UNIQUE ("text")) STRICT'),
    lambda sql: sql.replace(") STRICT", ', FOREIGN KEY ("id") REFERENCES "todo" ("id")) STRICT'),
    lambda sql: sql.replace(") STRICT", ")"),
])
def test_carry_refuses_a_change_of_keys_indexes_or_table_options(tmp_path, transform):
    source = tmp_path / "old.sqlite3"
    _old_todo(source, transform)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="columns, keys or indexes changed"):
        carry(source, tmp_path / "refused.sqlite3")
    assert source.read_bytes() == before
    assert not (tmp_path / "refused.sqlite3").exists()


@pytest.mark.parametrize("extra", [
    'CREATE INDEX extra_idx ON todo (text)',
    'CREATE TRIGGER extra_trigger BEFORE UPDATE ON todo BEGIN SELECT 1; END',
])
def test_carry_refuses_an_added_index_or_trigger(tmp_path, extra):
    source = tmp_path / "old.sqlite3"
    _old_todo(source)
    with closing(sqlite3.connect(source)) as db:
        db.execute(extra)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="columns, keys or indexes changed|added or removed"):
        carry(source, tmp_path / "refused.sqlite3")
    assert source.read_bytes() == before
    assert not (tmp_path / "refused.sqlite3").exists()


def test_carry_admits_the_delivered_state_into_an_older_coordination_store(tmp_path):
    """A store created before 'delivered' existed gains it with its rows intact."""
    from agent_comms.assignment_states import DeferredAssignment, DeliveredAssignment
    from agent_comms.coordination_cohort import accept_delivery_cohort
    from agent_comms.coordinator import Coordination
    from test_coordination_cohort import _root as cohort_root

    comms, coordinator, root_id, _ = cohort_root(tmp_path)
    message = comms.messaging.send_initial_cohort("sender", "#team", "read this")
    accepted = accept_delivery_cohort(comms.bus, root_id, message.seq, coordinator)
    assignment = accepted.value.assignments[0]
    paused = coordinator.assignments.transition_preengagement(
        assignment.assignment_id, DeferredAssignment, expected_revision=assignment.revision
    ).value
    coordinator.close()
    source = tmp_path / "comms" / "coordinator.sqlite"
    # Rewind the stored rules to their text from before the state was declared.
    with closing(sqlite3.connect(source, isolation_level=None)) as db:
        current = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE sql LIKE '%delivered%'"))
        assert set(current) == {"wake_claims", "claim_transition_frozen_facts"}
        db.execute("PRAGMA writable_schema=ON")
        for name, sql in current.items():
            db.execute("UPDATE sqlite_master SET sql=? WHERE name=?", (OLDER_RULES[name], name))
        db.execute("PRAGMA writable_schema=OFF")
    with closing(sqlite3.connect(source)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE wake_claims SET lifecycle=json_set(lifecycle,'$.kind','delivered')")
    target = tmp_path / "carried.sqlite"
    result = carry(source, target)
    assert sorted(result["carried"]) == ["claim_transition_frozen_facts", "wake_claims"]
    with Coordination(str(target)) as store:
        delivered = store.assignments.transition_preengagement(
            assignment.assignment_id, DeliveredAssignment, expected_revision=paused.revision
        ).value
        assert type(delivered.lifecycle) is DeliveredAssignment


def test_report_and_backup_use_one_snapshot_even_when_source_advances(tmp_path, monkeypatch):
    source = tmp_path / "old.sqlite3"
    _old_todo(source)
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA journal_mode=WAL")
    reader = StoreSchemaReport.read
    reads = []

    def advance_after_read(cls, db, path):
        assert db.in_transaction
        result = reader(db, path)
        reads.append(db)
        # A different writer advances the WAL after the source snapshot is held.
        with closing(sqlite3.connect(source)) as later:
            later.execute('CREATE INDEX later_idx ON todo (text)')
            later.commit()
        return result

    copy = tmp_path / "carried.sqlite3"
    # The accepted-copy observation uses the same method, but must not advance
    # the original again. Record the exact held handle only for the source.
    def source_only(cls, db, path):
        return advance_after_read(cls, db, path) if path == source else reader(db, path)

    monkeypatch.setattr(StoreSchemaReport, "read", classmethod(source_only))
    carry(source, copy)
    assert len(reads) == 1
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='later_idx'").fetchall() == []
        assert db.execute("SELECT text FROM todo").fetchall() == [("unfinished",)]
    with closing(sqlite3.connect(source)) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='later_idx'").fetchone()
