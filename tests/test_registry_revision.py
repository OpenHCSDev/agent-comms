"""Registry reads reuse unchanged declarations but follow external wire writes."""

from agent_comms.repo import Checkout
import json

import pytest

from agent_comms.nominal.field_codec import MalformedRecord
from agent_comms.registration import Registration
from agent_comms.store_files import AtomicFile
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread


@pytest.mark.parametrize("select", [
    lambda store: store.read_entry("worker"),
    lambda store: store.snapshot_with_authority(),
])
def test_dependent_selection_refuses_changed_authority_without_repeating_its_effect(tmp_path, select):
    from agent_comms.coordination_errors import StaleFence

    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread("worker", frozenset(), Checkout(tmp_path)))
    selected = select(registry.store)
    effects = []
    with selected.current():
        effects.append("original")
    registry.unregister("worker")
    with pytest.raises(StaleFence):
        with selected.current():
            effects.append("replayed")
    assert effects == ["original"]
    with type(selected).captured(lambda: select(registry.store)) as current:
        assert current.matches_current()


def test_an_entry_fence_ignores_other_threads_writes_but_not_its_own(tmp_path):
    # A start that clears its own triage input fences on its own entry: heartbeats of
    # every other live owner used to stale the whole-registry cut before it could write.
    from agent_comms.coordination_errors import StaleFence

    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread("worker", frozenset(), Checkout(tmp_path)))
    registry.register(Thread("neighbour", frozenset(), Checkout(tmp_path)))
    whole = registry.store.snapshot_with_authority()
    cut = registry.store.entries_cut(frozenset({"worker"}))
    registry.unregister("neighbour")
    with pytest.raises(StaleFence):
        with whole.current():
            pass
    with cut.current():
        pass
    registry.unregister("worker")
    with pytest.raises(StaleFence):
        with cut.current():
            pass

def test_missing_registry_version_is_rejected_without_rewriting_the_file(tmp_path):
    from agent_comms.errors import RelationViolationError
    from agent_comms.nominal.field_codec import FieldCodec
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
    cached = reader.store.cache.read_counts()
    for _ in range(3):
        Registration(path).store.read()
    reused = reader.store.cache.read_counts()
    assert reused.requests == cached.requests + 3
    assert reused.captures == cached.captures and reused.decodes == cached.decodes
    stored = dict(reader.store.read().statuses)

    # Another process stops one thread: an external replacement of the file.
    raw = json.loads(path.read_text())
    stopped = Registration(tmp_path / "other" / "registry.json")
    stopped.register(Thread(name='second', tags=frozenset(), worktree=Checkout(tmp_path)))
    stopped.unregister("second")
    raw["statuses"]["second"] = json.loads(stopped.store.path.read_text())["statuses"]["second"]
    AtomicFile(path).write_text(json.dumps(raw, indent=2))

    after = reader.all_threads()
    changed = reader.store.cache.read_counts()
    assert changed.captures == reused.captures + 1 and changed.decodes == reused.decodes + 1
    # Unchanged threads keep their decoded declarations and statuses.
    assert all(after[name] is before[name] for name in before)
    document = reader.store.read()
    assert document.statuses["first"] is stored["first"] and document.statuses["third"] is stored["third"]
    # A status read derives presence from the process or heartbeat on every read.
    assert reader.status("first") == statuses["first"] and reader.status("third") == statuses["third"]
    assert reader.status("second") == StoppedThreadStatus()
    # The result is the document a full decode of the same rows gives.
    document = reader.store.read()
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
    assert [thread.name for thread in reader.store.read().user_thread().existing()] == ["user"]

    # Only the worker's row changes; the user's declaration is reused, not decoded again.
    raw = json.loads(path.read_text())
    raw["last_seen"]["worker"] = raw["last_seen"]["worker"] + 1
    AtomicFile(path).write_text(json.dumps(raw, indent=2))

    assert [thread.name for thread in reader.store.read().user_thread().existing()] == ["user"]
