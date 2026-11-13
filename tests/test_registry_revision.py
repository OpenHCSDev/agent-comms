"""Registry reads reuse unchanged declarations but follow external wire writes."""


from contextlib import closing
from unittest.mock import patch

import pytest

from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.presence import PresenceRows, PresenceStore
from agent_comms.storage.row_mapping import RowMapping
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import InvalidRegistryDocument
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.threads import NativeThread, Thread
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.store_files import AtomicFile


def test_complete_declaration_keeps_row_reads_bounded_as_the_registry_grows(tmp_path):
    counts = []
    for size in (8, 64):
        root = tmp_path / str(size)
        registry = Registration(RegistryFile.path_in(root))
        for index in range(size):
            registry.declare(NativeThread(f"worker-{index}", frozenset(), Checkout(tmp_path)))
        with patch.object(RowMapping, "__getitem__", autospec=True,
                          side_effect=RowMapping.__getitem__) as reads:
            registry.declare(NativeThread("measured", frozenset(), Checkout(tmp_path)))
            counts.append(reads.call_count)
    assert counts[0] == counts[1]


def test_presence_updates_and_presentations_do_not_enumerate_unchanged_rows(tmp_path, monkeypatch):
    store = PresenceStore(tmp_path)
    store.set_rows({f"worker-{i}": float(i) for i in range(400)}, {})
    before = store.read()
    scope = before.seen.keyset()
    retained = PresenceRows().presented(before, scope)
    changed = retained.copy()
    changed["worker-0"] = 1000.0
    original = RowMapping.__iter__

    def require_changed_rows_only(rows):
        if len(rows):
            raise AssertionError("presence enumerated a complete row mapping")
        return original(rows)

    monkeypatch.setattr(RowMapping, "__iter__", require_changed_rows_only)
    store.set_rows(changed, retained)
    following = retained.presented(store.read(before), scope)
    assert retained["worker-0"] == 0.0
    assert following["worker-0"] == 1000.0
    assert following["worker-399"] == 399.0


def test_replacing_a_database_at_the_same_counter_does_not_reuse_its_cached_rows(tmp_path):
    first = Registration(RegistryFile.path_in(tmp_path / "first"))
    second = Registration(RegistryFile.path_in(tmp_path / "second"))
    first.register(NativeThread("original", frozenset(), Checkout(tmp_path)))
    second.register(NativeThread("replacement", frozenset(), Checkout(tmp_path)))
    before = first.store.read_revision()
    assert before.source.mark == second.store.rows.mark()
    staged = RegistryFile.path_in(tmp_path / "staged")
    staged.parent.mkdir()
    RegistryFile.copy_file(second.store.path, staged)
    # Publish a complete replacement, as rollback does; this is not a row update.
    AtomicFile(first.store.path).replace(staged)
    staged_presence = staged.parent / second.store.presence.path.name
    second.store.presence.copy_file(second.store.presence.path, staged_presence)
    AtomicFile(first.store.presence.path).replace(staged_presence)
    assert set(first.all_threads()) == {"replacement"}
    assert not first.store.has_revision(before.source.revision)


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


def test_thread_rows_keep_their_owned_wire_form_at_the_codec_boundary(thread_rows_wire, monkeypatch):
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.registry_document import StoredThreadRow, ThreadRows

    def decoded(_row):
        raise AssertionError("encoding stored thread rows decoded a declaration")

    monkeypatch.setattr(StoredThreadRow.thread, "func", decoded)
    assert FieldCodec.encode(ThreadRows.from_wire(thread_rows_wire)) == thread_rows_wire


def test_popping_a_thread_row_does_not_remove_it_when_decoding_fails(thread_rows_wire):
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.registry_document import ThreadRows

    malformed = {**thread_rows_wire["first"], FieldCodec.record_key(Thread, "tags"): 42}
    rows = ThreadRows.from_wire({"first": malformed})
    with pytest.raises(InvalidRegistryDocument):
        rows.popitem()
    assert "first" in rows
    assert rows.to_wire() == {"first": malformed}


def test_thread_row_copies_share_decodes_without_sharing_writes(thread_rows_wire):
    from dataclasses import replace

    from agent_comms.registry.registry_document import ThreadRows
    from agent_comms.registry.thread_text import GivenThreadText

    rows = ThreadRows.from_wire(thread_rows_wire)
    edit, frozen = rows.copy(), rows.freeze()
    learned = frozen["first"]
    assert rows["first"] is learned
    assert edit["first"] is learned
    edit["first"] = replace(learned, task=GivenThreadText("edited"))
    assert edit.differing(rows) == frozenset({"first"})
    assert frozen["first"] is learned
    assert rows["first"] is learned
    assert list(edit.only(frozenset({"second", "first"}))) == ["first", "second"]
    with pytest.raises(TypeError, match="read-only"):
        frozen.popitem()
    with pytest.raises(TypeError, match="read-only"):
        frozen.clear()


def test_birth_index_survives_copies_rename_and_removal_without_a_row_walk(thread_rows_wire, monkeypatch):
    from dataclasses import replace

    from agent_comms.registry.registry_document import ThreadRows
    from agent_comms.registry.thread_creation import CreationIdentityCollisionError

    rows = ThreadRows.from_wire(thread_rows_wire)
    first, second = rows["first"], rows["second"]
    frozen, edit = rows.freeze(), rows.copy()

    def enumerated(_rows):
        raise AssertionError("birth query enumerated thread rows")

    with monkeypatch.context() as patch:
        patch.setattr(ThreadRows, "__iter__", enumerated)
        assert first.created_at in edit.births()
        edit.require_distinct_births()
        edit["renamed"] = replace(first, name="renamed")
        with pytest.raises(CreationIdentityCollisionError):
            edit.require_distinct_births()
        del edit["first"]
        edit.require_distinct_births()
        del edit["renamed"]
        assert first.created_at not in edit.births()
        assert first.created_at in frozen.births()
        assert first.created_at in rows.births()
        assert second.created_at in edit.births()
    selected = rows.only(frozenset({"second"}))
    assert set(selected.births()) == {second.created_at}
    edit.clear()
    assert not edit.births()
    edit.require_distinct_births()


def test_birth_errors_stay_local_and_a_row_replacement_repairs_the_index(thread_rows_wire):
    from dataclasses import replace

    from agent_comms.registry.registry_document import ThreadRows

    rows = ThreadRows.from_wire({"bad": {"name": "bad", "not": "a thread"}})
    with pytest.raises(InvalidRegistryDocument):
        rows.births()
    valid = ThreadRows.from_wire(thread_rows_wire)["first"]
    rows["bad"] = replace(valid, name="bad")
    assert set(rows.births()) == {valid.created_at}
    rows.require_distinct_births()


def test_captures_and_row_commits_visit_only_changed_keys(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.registry_tables import RegistryThreads
    from agent_comms.registry.thread_text import GivenThreadText
    from agent_comms.storage.row_mapping import RowMapping

    registry = Registration(RegistryFile.path_in(tmp_path))
    for name in ("first", "second"):
        registry.register(NativeThread(name, frozenset(), Checkout(tmp_path)))
    original = registry.store.read_revision()
    document = original.document.copy()
    changed = replace(document.threads["second"], task=GivenThreadText("updated"))
    document.threads["second"] = changed
    iterate = RowMapping.__iter__

    def bounded_rows(rows):
        if len(rows):
            raise AssertionError("capture or commit enumerated an unchanged row collection")
        return iterate(rows)

    with monkeypatch.context() as patch:
        patch.setattr(RowMapping, "__iter__", bounded_rows)
        encoded = original.source.encoded(document, original.document)
        registry.store.rows.commit(encoded, original.source)
        refreshed = registry.store.rows.captured(original.source, registry.store.rows.revision())
    assert RegistryThreads.mapping_in(refreshed.raw)["second"] == FieldCodec.encode(changed)
    assert RegistryThreads.mapping_in(refreshed.raw)["first"] is RegistryThreads.mapping_in(original.source.raw)["first"]


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
