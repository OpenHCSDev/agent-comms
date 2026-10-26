"""Registry reads reuse unchanged declarations but follow external wire writes."""

from agent_comms.repo import Checkout
import json

import pytest

from agent_comms.field_codec import MalformedRecord
from agent_comms.registration import Registration
from agent_comms.store_files import AtomicFile
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread


def test_missing_registry_version_is_rejected_without_rewriting_the_file(tmp_path):
    from agent_comms.errors import RelationViolationError
    from agent_comms.field_codec import FieldCodec
    from agent_comms.registry_document import RegistryDocument

    path = tmp_path / "registry.json"
    raw = FieldCodec.encode(RegistryDocument())
    del raw["version"]
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    with pytest.raises(RelationViolationError, match="version"):
        Registration(path).snapshot()
    assert path.read_bytes() == before


def test_malformed_replacement_never_serves_stale_success(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registration(path)
    registry.register(Thread(name='worker', tags=frozenset(), worktree=Checkout(tmp_path)))
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
        writer.register(Thread(name=name, tags=frozenset(), worktree=Checkout(tmp_path)))
    reader = Registration(path)
    before = dict(reader.all_threads())
    statuses = {name: reader.status(name) for name in before}
    with reader.store.reading() as document:
        stored = dict(document.statuses)

    # Another process stops one thread: an external replacement of the file.
    raw = json.loads(path.read_text())
    stopped = Registration(tmp_path / "other" / "registry.json")
    stopped.register(Thread(name='second', tags=frozenset(), worktree=Checkout(tmp_path)))
    stopped.unregister("second")
    raw["statuses"]["second"] = json.loads(stopped.store.path.read_text())["statuses"]["second"]
    AtomicFile(path).write_text(json.dumps(raw, indent=2))

    after = reader.all_threads()
    # Unchanged threads keep their decoded declarations and statuses.
    assert all(after[name] is before[name] for name in before)
    with reader.store.reading() as document:
        assert document.statuses["first"] is stored["first"] and document.statuses["third"] is stored["third"]
    # A status read derives presence from the process or heartbeat on every read.
    assert reader.status("first") == statuses["first"] and reader.status("third") == statuses["third"]
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
        registry.register(Thread(name=name, tags=frozenset(), worktree=Checkout(tmp_path)))
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


def test_a_reused_revision_still_finds_its_user_thread(tmp_path):
    from agent_comms.thread_identity import UserThreadRole

    path = tmp_path / "registry.json"
    writer = Registration(path)
    writer.register(Thread(name="user", tags=frozenset(), worktree=Checkout(tmp_path),
                           role=UserThreadRole))
    writer.register(Thread(name="worker", tags=frozenset(), worktree=Checkout(tmp_path)))
    reader = Registration(path)
    with reader.store.reading() as document:
        assert [thread.name for thread in document.user_thread().existing()] == ["user"]

    # Only the worker's row changes; the user's declaration is reused, not decoded again.
    raw = json.loads(path.read_text())
    raw["last_seen"]["worker"] = raw["last_seen"]["worker"] + 1
    AtomicFile(path).write_text(json.dumps(raw, indent=2))

    with reader.store.reading() as document:
        assert [thread.name for thread in document.user_thread().existing()] == ["user"]
