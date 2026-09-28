"""Saved documents, independent snapshots, and disposable activity projection."""

import json
from unittest.mock import patch

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.activity_checkpoint import ActivityCheckpointStore
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import LockedStore
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoStore
from agent_comms.shared_ledger import SharedLedger
from agent_comms.store_files import _store_lock


@pytest.mark.parametrize("store_type", [RuntimeInfoStore, SharedLedger])
def test_document_readers_share_lock_and_exclude_updates(tmp_path, store_type):
    store = store_type(tmp_path / store_type.filename)
    with store.reading() as first:
        assert first == store_type(store.path).read() == {}
        with pytest.raises(BlockingIOError), _store_lock(store.path, blocking=False):
            pass
    store.update(lambda rows: {**rows})
    assert store.read() == {}


def test_saved_runtime_dates_and_fields_survive_rename_and_reopen(tmp_path):
    store = RuntimeInfoStore(tmp_path / RuntimeInfoStore.filename)
    saved = {
        "old": {
            "thread": "old",
            "model": "configured/model",
            "session_name": "saved",
            "context_used": 123,
            "context_size": 1000,
            "ts": 456.25,
        },
        "undated": {"thread": "undated"},
    }
    store.path.write_text(json.dumps(saved))
    original = store.path.read_bytes()
    assert store.read()["undated"].timestamp == 0
    assert store.path.read_bytes() == original
    store.rename_thread("old", "new")
    reopened = RuntimeInfoStore(store.path).read()
    assert FieldCodec.encode(reopened["new"]) == {**saved["old"], "thread": "new"}
    assert reopened["undated"].timestamp == 0
    before = store.path.stat()
    store.remove("absent")
    assert store.path.stat() == before


def test_free_form_ledger_has_no_retained_mutable_mirror(tmp_path):
    store = SharedLedger(tmp_path / SharedLedger.filename)
    saved = {
        "nested": {"old": ["old", None, True, 9, {"free": "old is prose"}]},
        "unknown-domain": {"state": "anything", "value": 1.25},
    }
    store.path.write_text(json.dumps(saved))
    snapshot = store.read()
    snapshot["nested"].clear()
    assert SharedLedger(store.path).read() == saved
    SharedLedger(store.path).merge({"other": [1, 2]}, "other")
    assert store.rename_thread("old", "new") == 2
    assert store.read()["nested"] == {"new": ["new", None, True, 9, {"free": "old is prose"}]}
    assert store.remove_thread("new") == 1
    assert store.read() == {**saved, "nested": {}, "other": [1, 2], "last_updated_by": "other"}


@pytest.mark.parametrize(
    "store_type,operation,initial",
    [
        (
            RuntimeInfoStore,
            lambda s: s.set(AgentRuntimeInfo("new")),
            '{"old": {"thread": "old", "ts": 1}}',
        ),
        (
            SharedLedger,
            lambda s: s.merge({"new": "value"}, "writer"),
            '{"old": {"data": [1, null]}}',
        ),
    ],
)
def test_authoritative_documents_keep_bytes_on_failed_publication(
    tmp_path, monkeypatch, store_type, operation, initial
):
    store = store_type(tmp_path / store_type.filename)
    store.path.write_text(initial)

    def fail(source, destination):
        raise OSError("publication failed")

    monkeypatch.setattr("agent_comms.locked_store._replace_snapshot", fail)
    with pytest.raises(OSError, match="publication failed"):
        operation(store)
    assert store.path.read_text() == initial


@pytest.mark.parametrize(
    "damage",
    [
        lambda value: value.update(schema=2),
        lambda value: value.update(offset=-1),
        lambda value: value.update(tail="wrong"),
        lambda value: value["latest"]["worker"].update(thread="impostor"),
        lambda value: value["latest"]["worker"].pop("ts"),
    ],
)
def test_invalid_checkpoint_rebuilds_from_unchanged_authoritative_log(tmp_path, damage):
    log = ActivityLog(tmp_path / "activity.jsonl")
    event = Activity("worker", ActivityState.WORKING, "saved", 123.0)
    log.emit(event)
    assert log.all_current(active=frozenset({"worker"}))["worker"] == event
    original = log._path.read_bytes()
    checkpoint = json.loads(log.checkpoint.path.read_text())
    damage(checkpoint)
    log.checkpoint.path.write_text(json.dumps(checkpoint))
    reopened = ActivityLog(log._path)
    assert reopened.all_current(active=frozenset({"worker"}))["worker"] == event
    assert log._path.read_bytes() == original
    assert ActivityCheckpointStore(log.checkpoint.path).read().latest == {"worker": event}


def test_complete_snapshot_replacement_does_not_decode_discarded_document(tmp_path):
    store = SharedLedger(tmp_path / SharedLedger.filename)
    store.path.write_text("invalid discarded snapshot")
    with patch.object(LockedStore, "_read_unlocked", side_effect=AssertionError("old read")):
        store.replace({"captured": [1, 2]})
    assert store.read() == {"captured": [1, 2]}
