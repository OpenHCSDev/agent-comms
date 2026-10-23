"""Saved documents, independent snapshots, and disposable activity projection."""

import json
from unittest.mock import patch

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import DocumentChange
from agent_comms.runtime_info import RuntimeInfoStore
from agent_comms.shared_ledger import SharedLedger
from test_locked_store import assert_writer_excluded


@pytest.mark.parametrize("store_type", [RuntimeInfoStore, SharedLedger])
def test_document_readers_share_lock_and_exclude_updates(tmp_path, store_type):
    store = store_type(tmp_path / store_type.filename)
    with store.reading() as first:
        assert first == store_type(store.path).read() == store.empty()
        assert_writer_excluded(store.path)
    store.update(DocumentChange.to)
    assert store.read() == store.empty()


def test_saved_runtime_dates_and_fields_survive_rename_and_reopen(tmp_path, *, thread_registration):
    store = RuntimeInfoStore.at(tmp_path)
    saved = {
        "old": {
            "thread": "old",
            "model": "configured/model",
            "context_used": 123,
            "context_size": 1000,
            "ts": 456.25,
        },
    }
    store.path.write_text(json.dumps(saved))
    original = store.path.read_bytes()
    assert store.read().observations["old"].timestamp == 456.25
    assert store.path.read_bytes() == original
    store.rename_thread("old", "new")
    assert RuntimeInfoStore(store.path).read().observations["new"].timestamp == 456.25
    assert json.loads(store.path.read_text()) == {"new": {**saved["old"], "thread": "new"}}
    before = store.path.stat()
    store.remove_threads((thread_registration.new_thread('absent', frozenset(), str(tmp_path)),))
    assert store.path.stat() == before


@pytest.mark.parametrize("timestamp", [None, "missing", True, float("nan")])
def test_runtime_observation_requires_original_finite_date(tmp_path, timestamp):
    store = RuntimeInfoStore.at(tmp_path)
    row = {"thread": "undated"}
    if timestamp is not None:
        row["ts"] = timestamp
    store.path.write_text(json.dumps({"undated": row}))
    original = store.path.read_bytes()
    with patch("time.time", side_effect=AssertionError("Reading cannot capture an observation")):
        with pytest.raises(ValueError):
            store.read()
    assert store.path.read_bytes() == original


def test_free_form_ledger_has_no_retained_mutable_mirror(tmp_path, *, thread_registration):
    store = SharedLedger.at(tmp_path)
    saved = {
        "nested": {"old": ["old", None, True, 9, {"free": "old is prose"}]},
        "unknown-domain": {"state": "anything", "value": 1.25},
    }
    store.path.write_text(json.dumps(saved))
    snapshot = store.read()
    snapshot.value["nested"].value.clear()
    assert FieldCodec.encode(SharedLedger(store.path).read()) == saved
    SharedLedger(store.path).merge({"other": [1, 2]}, "other")
    assert store.rename_thread("old", "new") == 2
    assert FieldCodec.encode(store.read())["nested"] == {"new": ["new", None, True, 9, {"free": "old is prose"}]}
    store.remove_threads((thread_registration.new_thread('new', frozenset(), str(tmp_path)),))
    assert FieldCodec.encode(store.read()) == {**saved, "nested": {}, "other": [1, 2], "last_updated_by": "other"}
    unchanged = store.path.stat()
    store.remove_threads((thread_registration.new_thread('absent', frozenset(), str(tmp_path)),))
    assert store.path.stat() == unchanged


