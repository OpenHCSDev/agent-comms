"""Registry reads reuse unchanged declarations but follow external wire writes."""


from contextlib import closing

import pytest

from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import InvalidRegistryDocument
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.threads import NativeThread, Thread
from agent_comms.storage.locked_store import RegistryFile


def test_retained_registry_keeps_wal_rows_and_revision_ignores_checkpoints(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path / "source"))
    registry.register(NativeThread("first", frozenset(), Checkout(tmp_path)))
    # Keep an older SQLite read open so the second declaration remains in the WAL.
    with closing(RegistryFile.read_snapshot(registry.store.path)):
        registry.register(NativeThread("second", frozenset(), Checkout(tmp_path)))
        captured = registry.store.read_revision()
        retained = registry.store.retain(tmp_path / "retained")
        assert set(retained.document.threads) == {"first", "second"}
        assert registry.store.has_revision(captured.source.revision)
    with closing(RegistryFile.writer(registry.store.path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert registry.store.has_revision(captured.source.revision)
    registry.unregister("second")
    assert not registry.store.has_revision(captured.source.revision)
    assert set(RegistryStore.at(tmp_path / "retained").read().threads) == {"first", "second"}


@pytest.mark.parametrize("select", [
    lambda store: store.read_entry("worker"),
    lambda store: store.snapshot_with_authority(),
])
def test_dependent_selection_refuses_changed_authority_without_repeating_its_effect(tmp_path, select):
    from agent_comms.coordination.errors import StaleFence

    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.register(NativeThread("worker", frozenset(), Checkout(tmp_path)))
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
    from agent_comms.coordination.errors import StaleFence

    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.register(NativeThread("worker", frozenset(), Checkout(tmp_path)))
    registry.register(NativeThread("neighbour", frozenset(), Checkout(tmp_path)))
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
    from agent_comms.registry.registry_document import RegistryDocument

    path = RegistryFile.path_in(tmp_path)
    raw = FieldCodec.encode(RegistryDocument())
    del raw["version"]
    RegistryStore(path).rewrite_stored(lambda _, stored=raw: stored)
    before = RegistryStore(path).rows.mark()
    with pytest.raises(RelationViolationError, match="version"):
        Registration(path).snapshot()
    assert RegistryStore(path).rows.mark() == before


def test_malformed_replacement_never_serves_stale_success(tmp_path):
    path = RegistryFile.path_in(tmp_path)
    registry = Registration(path)
    registry.register(NativeThread(name='worker', tags=frozenset(), worktree=Checkout(tmp_path)))
    saved = registry.store.stored()
    registry.require("worker")
    # A row that names another thread: the stored form no decoder accepts.
    registry.store.rewrite_stored(lambda raw: {**raw, "threads": {"worker": {"name": "other"}}})
    for _ in range(2):
        with pytest.raises(InvalidRegistryDocument):
            registry.require("worker")
    registry.store.rewrite_stored(lambda _: saved)
    assert registry.require("worker").name == "worker"


def test_a_new_revision_decodes_only_the_threads_that_changed(tmp_path):
    from agent_comms.registry.registry_document import RegistryDocument

    path = RegistryFile.path_in(tmp_path)
    writer = Registration(path)
    for name in ("first", "second", "third"):
        writer.register(NativeThread(name=name, tags=frozenset(), worktree=Checkout(tmp_path)))
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
    raw = RegistryStore(path).stored()
    stopped = Registration(RegistryFile.path_in(tmp_path / "other"))
    stopped.register(NativeThread(name='second', tags=frozenset(), worktree=Checkout(tmp_path)))
    stopped.unregister("second")
    raw["statuses"]["second"] = stopped.store.stored()["statuses"]["second"]
    RegistryStore(path).rewrite_stored(lambda _, stored=raw: stored)

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


@pytest.fixture
def thread_rows_wire(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    for name in ("first", "second"):
        registry.register(NativeThread(name, frozenset(), Checkout(tmp_path)))
    return registry.store.read().threads.to_wire()


@pytest.mark.parametrize("view", [lambda rows: rows, lambda rows: rows.copy(), lambda rows: rows.freeze()])
def test_reading_thread_rows_reuses_their_existing_wire_values(thread_rows_wire, view):
    from agent_comms.registry.registry_document import ThreadRows

    wire = thread_rows_wire
    rows = view(ThreadRows.from_wire(wire))
    for name in rows:
        assert rows[name].name == name
    encoded = rows.to_wire()
    assert encoded == wire
    assert all(encoded[name] is wire[name] for name in rows)


def test_assigning_and_removing_thread_rows_changes_only_the_written_rows(thread_rows_wire):
    from dataclasses import replace

    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.registry_document import ThreadRows
    from agent_comms.registry.thread_text import GivenThreadText

    wire = thread_rows_wire
    rows = ThreadRows.from_wire(wire)
    changed = replace(rows["second"], task=GivenThreadText("updated"))
    rows["second"] = changed
    encoded = rows.to_wire()
    assert encoded["first"] is wire["first"]
    assert FieldCodec.decode(Thread, encoded["second"]) == changed
    del rows["second"]
    assert rows.to_wire() == {"first": wire["first"]}


def test_a_changed_revision_is_checked_as_a_whole(tmp_path):
    from agent_comms.errors import RelationViolationError

    path = RegistryFile.path_in(tmp_path)
    registry = Registration(path)
    for name in ("first", "second"):
        registry.register(NativeThread(name=name, tags=frozenset(), worktree=Checkout(tmp_path)))
    registry.require("first")
    # A declaration without its status: the unchanged rows do not excuse it.
    raw = RegistryStore(path).stored()
    del raw["statuses"]["second"]
    RegistryStore(path).rewrite_stored(lambda _, stored=raw: stored)
    with pytest.raises(RelationViolationError):
        registry.require("first")
    # A changed row that does not decode is rejected too.
    raw = RegistryStore(path).stored()
    raw["statuses"]["second"] = {"kind": "no-such-status"}
    RegistryStore(path).rewrite_stored(lambda _, stored=raw: stored)
    with pytest.raises(RelationViolationError):
        registry.require("first")


def test_a_reused_revision_still_finds_its_user_thread(tmp_path):
    from agent_comms.registry.thread_identity import UserThreadRole

    path = RegistryFile.path_in(tmp_path)
    writer = Registration(path)
    writer.register(NativeThread(name="user", tags=frozenset(), worktree=Checkout(tmp_path),
                           role=UserThreadRole))
    writer.register(NativeThread(name="worker", tags=frozenset(), worktree=Checkout(tmp_path)))
    reader = Registration(path)
    assert [thread.name for thread in reader.store.read().user_thread().existing()] == ["user"]

    # Only the worker's row changes; the user's declaration is reused, not decoded again.
    raw = RegistryStore(path).stored()
    raw["threads"]["worker"]["title"] = "retitled"
    RegistryStore(path).rewrite_stored(lambda _, stored=raw: stored)

    assert [thread.name for thread in reader.store.read().user_thread().existing()] == ["user"]
