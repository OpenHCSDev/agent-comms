"""Registry reads reuse unchanged declarations but follow external wire writes."""

import json

import pytest

from agent_comms.field_codec import MalformedRecord
from agent_comms.registration import Registration
from agent_comms.store_files import AtomicFile
from agent_comms.thread_status import StoppedThreadStatus


def test_malformed_replacement_never_serves_stale_success(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registration(path)
    registry.register(registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    saved = path.read_text()
    registry.require("worker")
    AtomicFile(path).write_text("{")
    for _ in range(2):
        with pytest.raises(MalformedRecord):
            registry.require("worker")
    AtomicFile(path).write_text(saved)
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
    AtomicFile(path).write_text(json.dumps(raw, indent=2))

    after = reader.all_threads()
    # Unchanged threads keep their decoded declarations and statuses.
    assert all(after[name] is before[name] for name in before)
    assert reader.status("first") is statuses["first"] and reader.status("third") is statuses["third"]
    assert reader.status("second") == StoppedThreadStatus()
    # The result is the document a full decode of the same rows gives.
    with reader.store.reading() as document:
        assert document == RegistryDocument.from_wire(raw)
        assert list(document.threads) == list(raw["threads"])


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
    AtomicFile(path).write_text(json.dumps(raw, indent=2))
    with pytest.raises(RelationViolationError):
        registry.require("first")
    # A changed row that does not decode is rejected too.
    raw = json.loads(path.read_text())
    raw["statuses"]["second"] = {"kind": "no-such-status"}
    AtomicFile(path).write_text(json.dumps(raw, indent=2))
    with pytest.raises(RelationViolationError):
        registry.require("first")
