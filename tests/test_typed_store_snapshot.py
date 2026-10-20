"""Every declared SQL store can read before its first write without filesystem work."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.input_disposition import InputDispositionRow, InputDispositions
from agent_comms.owner_lifecycle import OwnerReleaseStore
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.wire_log import WireLog


@pytest.mark.parametrize("make_store", (
    InputDispositions,
    OwnerReleaseStore,
    lambda root: ExternalTranscript(root, RecordedThreadIncarnation("cli", 1.0)),
    lambda root: WakeCandidateIndex(WireLog(root / "bus.jsonl")),
), ids=("inputs", "releases", "transcript", "wake"))
def test_missing_snapshot_has_declared_read_only_schema_without_creating_root(tmp_path, make_store):
    root = tmp_path / "never-written"
    store = make_store(root)
    assert not root.exists()
    with store.snapshot() as connection:
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA database_list").fetchone()[2] == ""
        store.require_schema(connection)
        for row in store.rows:
            assert row.select(connection) == []
        with pytest.raises(sqlite3.OperationalError):
            store.rows[0].delete(connection, where="1")
    assert not root.exists()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_first_write_does_not_change_the_opened_empty_snapshot(tmp_path):
    store = InputDispositions(tmp_path / "new")
    with ThreadPoolExecutor(max_workers=1) as writer:
        with store.snapshot() as connection:
            recorded = writer.submit(store.record, "first", seq=None, owner="kid",
                                     claim_scope=1, target="kid", text="first")
            assert recorded.result(timeout=2)
            assert InputDispositionRow.select(connection) == []
    assert store.lookup("first").source_text == "first"


def test_change_marker_publishes_only_committed_row_changes(tmp_path):
    store = InputDispositions(tmp_path)
    marker, = store.source_paths(tmp_path)
    with store.writing():
        pass
    assert not marker.exists()
    store.record("first", seq=None, owner="kid", claim_scope=1, target="kid", text="first")
    changed = marker.stat().st_mtime_ns
    with store.snapshot() as connection:
        assert InputDispositionRow.select(connection)
    with store.writing() as connection:
        InputDispositionRow.delete(connection, where="0")
    assert marker.stat().st_mtime_ns == changed
    with pytest.raises(ValueError):
        with store.writing() as connection:
            InputDispositionRow.delete(connection)
            raise ValueError("rollback")
    assert marker.stat().st_mtime_ns == changed and store.lookup("first").source_text == "first"
    store.record("second", seq=None, owner="kid", claim_scope=1, target="kid", text="second")
    assert marker.stat().st_mtime_ns != changed


def test_existing_undeclared_schema_is_not_treated_as_missing(tmp_path):
    store = InputDispositions(tmp_path)
    store.path.touch(mode=0o600)
    with pytest.raises(RelationViolationError):
        with store.snapshot():
            pytest.fail("An existing invalid file must not become an empty store")
    assert store.path.stat().st_size == 0
