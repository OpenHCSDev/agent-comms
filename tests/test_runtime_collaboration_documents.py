"""Saved documents, independent snapshots, and disposable activity projection."""

import json
import time
from unittest.mock import patch

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.activity_checkpoint import ActivityCheckpointStore
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import LockedStore
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoStore
from agent_comms.shared_ledger import SharedLedger
from agent_comms.store_files import _store_lock

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_installed_native_observation_producers_preserve_dates_without_input(native_backend, monkeypatch):
    import asyncio
    import os

    from agent_comms.comms import Comms
    from agent_comms.native_session_prepare import NativeSessionPreparation
    from delivery_owner_fixture import canonical_agent

    native = native_backend
    arguments = ["--provider=response-local", "--model=fixture", "--thinking=off", "--offline", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-tools"]
    # Establish the saved native model/settings through its real owner. A
    # never-opened fresh journal legitimately gains configuration entries.
    await NativeSessionPreparation.open(
        native.persistent, "pi", arguments, worktree=str(native.project),
        environment=dict(os.environ), session_file=str(native.session),
    )
    await native.persistent.close()
    history = native.session.read_bytes()
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    agent = canonical_agent(
        Comms(native.root), auto_wake=False, runtime_enabled=False,
        agent_args=arguments,
    )
    child = None
    try:
        async with asyncio.timeout(15):
            created = await agent.new_session(cwd=str(native.project), mcp_servers=[])
            name = created.session_id
            agent._comms.threads.attach_session(name, str(native.session))
            thread = agent._comms.registry.require(name)
            before = time.time()
            prepared = await agent.turns.prepare_selected_session(name, thread)
            assert prepared.model.display_name == "response-local/fixture"
            child = agent.turns.persistent_backends[name].custody.child.proc
            assert child.alive()
            before = time.time()
            agent._comms.agents.set_agent_info(name, model=prepared.model.display_name, context_size=prepared.model.context_window)
            captured = agent._comms.agents.agent_info_of(name)
            assert before <= captured.timestamp <= time.time()
            store = agent._comms.agents.runtime_info
            original = store.path.read_bytes()
            with patch("time.time", side_effect=AssertionError("Reading cannot capture an observation")):
                assert RuntimeInfoStore(store.path).read()[name] == captured
                store.rename_thread(name, "renamed-observation")
                assert RuntimeInfoStore(store.path).read()["renamed-observation"].timestamp == captured.timestamp
            assert json.loads(original)[name]["ts"] == captured.timestamp
            assert native.session.read_bytes() == history
            assert native.provider.posts == len(native.saved_inputs()) == 0
    finally:
        await agent.shutdown()
    assert child is not None and not child.alive()


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
    }
    store.path.write_text(json.dumps(saved))
    original = store.path.read_bytes()
    assert store.read()["old"].timestamp == 456.25
    assert store.path.read_bytes() == original
    store.rename_thread("old", "new")
    reopened = RuntimeInfoStore(store.path).read()
    assert FieldCodec.encode(reopened["new"]) == {**saved["old"], "thread": "new"}
    before = store.path.stat()
    store.remove("absent")
    assert store.path.stat() == before


@pytest.mark.parametrize("timestamp", [None, "missing", True, float("nan")])
def test_runtime_observation_requires_original_finite_date(tmp_path, timestamp):
    store = RuntimeInfoStore(tmp_path / RuntimeInfoStore.filename)
    row = {"thread": "undated"}
    if timestamp is not None:
        row["ts"] = timestamp
    store.path.write_text(json.dumps({"undated": row}))
    original = store.path.read_bytes()
    with patch("time.time", side_effect=AssertionError("Reading cannot capture an observation")):
        with pytest.raises(ValueError):
            store.read()
    assert store.path.read_bytes() == original


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
            lambda s: s.set(AgentRuntimeInfo("new", timestamp=2.0)),
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
