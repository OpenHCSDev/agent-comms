"""Registry reads reuse unchanged declarations but follow external wire writes."""

import json
from unittest.mock import patch

import pytest

from agent_comms.field_codec import MalformedRecord
from agent_comms.registration import Registration
from agent_comms.store_files import _atomic_write_text
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def test_repeated_reads_parse_once_and_observe_external_changes(tmp_path):
    path = tmp_path / "registry.json"
    writer = Registration(path)
    writer.register(writer.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    reader = Registration(path)
    original = reader.require("worker")
    with patch("agent_comms.registry_store.json.loads", wraps=json.loads) as loads:
        for _ in range(20):
            assert reader.require("worker") is original
            assert reader.status("worker") == RunningThreadStatus()
            assert reader.all_threads()["worker"] is original
        assert loads.call_count == 0
        writer.unregister("worker")
        assert reader.status("worker") == StoppedThreadStatus()
    # External atomic replacement invalidates both identities and status.
    contents = path.read_text().replace('"worker"', '"renamed"')
    _atomic_write_text(path, contents)
    assert "worker" not in reader
    assert reader.require("renamed").name == "renamed"
    path.unlink()
    assert not reader.all_threads()


def test_malformed_replacement_never_serves_stale_success(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registration(path)
    registry.register(registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    saved = path.read_text()
    registry.require("worker")
    _atomic_write_text(path, "{")
    for _ in range(2):
        with pytest.raises(MalformedRecord):
            registry.require("worker")
    _atomic_write_text(path, saved)
    assert registry.require("worker").name == "worker"


def test_a_new_revision_decodes_only_the_threads_that_changed(tmp_path):
    from agent_comms.registry_document import RegistryDocument

    path = tmp_path / "registry.json"
    writer = Registration(path)
    for name in ("first", "second", "third"):
        writer.register(writer.new_thread(name=name, tags=frozenset(), worktree=str(tmp_path)))
    reader = Registration(path)
    before = dict(reader.all_threads())
    statuses = {name: reader.status(name) for name in before}

    # Another process stops one thread: an external replacement of the file.
    raw = json.loads(path.read_text())
    stopped = Registration(tmp_path / "other" / "registry.json")
    stopped.register(stopped.new_thread(name='second', tags=frozenset(), worktree=str(tmp_path)))
    stopped.unregister("second")
    raw["statuses"]["second"] = json.loads(stopped.store.path.read_text())["statuses"]["second"]
    _atomic_write_text(path, json.dumps(raw, indent=2))

    after = reader.all_threads()
    # Unchanged threads keep their decoded declarations and statuses.
    assert all(after[name] is before[name] for name in before)
    assert reader.status("first") is statuses["first"] and reader.status("third") is statuses["third"]
    assert reader.status("second") == StoppedThreadStatus()
    # The result is the document a full decode of the same rows gives.
    with reader.store.reading() as document:
        assert document == RegistryDocument.from_wire(raw)
        assert list(document.threads) == list(raw["threads"])


def test_sender_bindings_are_lazy_immutable_and_owned_by_the_snapshot(tmp_path):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.registry_provenance import RegistryProvenance
    from agent_comms.routing import DeliveryScope

    registry = Registration(tmp_path / "registry.json")
    registry.register(registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    snapshot = registry.snapshot()
    historical = RegistryProvenance.capture(snapshot)
    encoded = FieldCodec.encode(historical)
    for namespace in (snapshot, historical):
        with patch.object(DeliveryScope, "sender_lookups", wraps=DeliveryScope.sender_lookups) as lookups:
            assert "sender_bindings" not in namespace.__dict__
            first = namespace.sender_bindings
            assert namespace.sender_bindings is first
            assert lookups.call_count == len(tuple(namespace.named_members()))
        with pytest.raises(TypeError):
            first["senders"] = "{}"
        assert FieldCodec.encode(historical) == encoded
        assert json.loads(first["unrecorded"]) == []
    assert registry.snapshot() is snapshot
    registry.heartbeat("worker")
    newer = registry.snapshot()
    assert newer is not snapshot
    assert "sender_bindings" not in newer.__dict__
    assert newer.sender_bindings == snapshot.sender_bindings
    assert newer.sender_bindings is not snapshot.sender_bindings


def test_a_changed_revision_is_checked_as_a_whole(tmp_path):
    from agent_comms.errors import RelationViolationError

    path = tmp_path / "registry.json"
    registry = Registration(path)
    for name in ("first", "second"):
        registry.register(registry.new_thread(name=name, tags=frozenset(), worktree=str(tmp_path)))
    registry.require("first")
    # A declaration without its status: the unchanged rows do not excuse it.
    raw = json.loads(path.read_text())
    del raw["statuses"]["second"]
    _atomic_write_text(path, json.dumps(raw, indent=2))
    with pytest.raises(RelationViolationError):
        registry.require("first")
    # A changed row that does not decode is rejected too.
    raw = json.loads(path.read_text())
    raw["statuses"]["second"] = {"kind": "no-such-status"}
    _atomic_write_text(path, json.dumps(raw, indent=2))
    with pytest.raises(RelationViolationError):
        registry.require("first")
