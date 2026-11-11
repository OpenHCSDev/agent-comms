"""Ownership and protocol experiments for the Registration/A8 document boundary."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.messaging.routing import NoTurnRouting
from agent_comms.messaging.snapshot_slices import ThreadSlice
from agent_comms.nominal.field_codec import FieldCodec, TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import ThreadRows
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import ExternalThread, NativeThread, Thread
from agent_comms.storage.locked_store import DocumentChange, RegistryFile


def owner(path):
    return NativeThread("owner", frozenset(), Checkout(path), process_identity=ProcessIdentity.capture(os.getpid()), created_at=10.0)


def test_edit_owns_turn_and_document_owns_later_changes(tmp_path, *, thread_registration):
    thread_registration.register(owner(tmp_path))
    before = thread_registration.snapshot().owner_identity("owner")
    with thread_registration.store.editing() as edit:
        leased, generation = edit.lease_turn(edit.document.threads["owner"], TurnId.mint(), NoTurnRouting())
    assert generation == before.generation
    assert leased.turn_generation == 1
    thread_registration.release_turn(leased.turn_lease)
    document = thread_registration.store.read().copy()
    document.rename("owner", "renamed")
    document.unregister("renamed")
    document.begin_delete("renamed")
    assert document.remove_originals((document.threads["renamed"],)) == {"renamed": ()}
    assert not document.threads
    assert not list(tmp_path.iterdir())


def test_store_owns_document_and_failed_edit_cannot_leak_into_cache(tmp_path):
    registration = Registration(RegistryFile.path_in(tmp_path))
    registration.register(owner(tmp_path))
    prior = registration.snapshot()
    revision = registration.store.rows.mark()
    with pytest.raises(RuntimeError), registration.store.editing() as edit:
        edit.document.unregister("owner")
        raise RuntimeError("aborted")
    assert registration.snapshot() == prior
    assert registration.store.rows.mark() == revision


def test_one_thread_entry_decodes_only_that_thread_and_matches_the_document(tmp_path):
    registration = Registration(RegistryFile.path_in(tmp_path))
    registration.register(owner(tmp_path))
    registration.register(replace(owner(tmp_path), name="other", created_at=11.0))
    registration.rename("owner", "renamed")
    document = registration.store.read()
    for name in ("renamed", "owner", "other"):
        assert registration.entry(name).entry == document.entry(name)
    # A separate writer changes another row, without touching this reader's cache.
    external = Registration(registration.store.path)
    external.register(replace(external.require("other"), title=GivenThreadText("external revision")))
    raw = external.store.stored()
    read = registration.entry("owner")
    assert read.entry == document.entry("renamed") and read.entry.thread.name == "renamed"
    assert read.revision == registration.store.revision_unlocked()
    reopened = Registration(registration.store.path)
    assert reopened.entry("owner").entry.thread is read.entry.thread
    snapshot = reopened.snapshot()
    assert snapshot.threads["renamed"] is read.entry.thread
    assert reopened.snapshot() is snapshot
    # Another thread's malformed declaration is not this thread's entry.
    raw["threads"]["other"]["created_at"] = "not a time"
    external.store.rewrite_stored(lambda _: raw)
    assert registration.entry("renamed").entry == document.entry("renamed")
    with pytest.raises(RelationViolationError):
        registration.entry("other")
    with pytest.raises(UnregisteredThreadError):
        registration.entry("missing")


def test_a_document_field_without_a_thread_slice_is_refused_at_definition():
    with pytest.raises(TypeError):
        @ThreadSlice.required
        @dataclass
        class Unsliced:
            rows: dict = field(default_factory=dict)


def test_registry_update_persists_across_reopen(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.register(owner(tmp_path))

    def change(original):
        modified = original.copy()
        modified.threads["owner"] = replace(modified.threads["owner"], title=GivenThreadText("a8 update"))
        return DocumentChange.to(modified)

    registry.store.update(change)
    assert Registration(registry.store.path).require("owner").title == GivenThreadText("a8 update")


def test_new_thread_field_has_no_second_registry_decoder_roster(tmp_path, *, thread_registration):
    @dataclass(frozen=True, slots=True)
    class ExtendedThread(Thread):
        external_reference: str = "default"

    raw = FieldCodec.encode(thread_registration.declare(owner(tmp_path))) | {
        "external_reference": "declared here",
        Thread.family_discriminator: ExtendedThread.declared_name,  # a member names itself
    }
    loaded = FieldCodec.decode(ExtendedThread, raw)
    assert loaded.external_reference == "declared here"
    assert loaded.to_wire()["external_reference"] == "declared here"


@pytest.mark.skipif(os.name != "posix", reason="nonblocking shared flock acceptance")
def test_current_cut_excludes_writer_and_releases_for_later_metadata(tmp_path):
    from test_locked_store import assert_writer_excluded
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.register(owner(tmp_path))
    other = RegistryStore(registry.store.path)
    with registry.store.current_snapshot() as cut:
        assert other.read().threads["owner"].name == "owner"
        assert_writer_excluded(other.path)
    registry.register(replace(cut.snapshot.require("owner"), task=GivenThreadText("changed")))
    assert registry.require("owner").task == GivenThreadText("changed")
    assert cut.snapshot.require("owner").task != registry.require("owner").task


def test_lifecycle_document_survives_fresh_process_restart(tmp_path):
    first = TurnId.mint()
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("registration_lifecycle_child.py")),
         str(tmp_path / "current"), str(first)],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src")),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    steps = json.loads(result.stdout)
    assert len(steps) == 10
    assert steps[2]["threads"]["owner"]["title"] == "metadata"
    assert steps[3]["turns"]["owner"]["id"] == str(first)
    assert steps[4]["turns"]["owner"] is None
    assert steps[5]["threads"]["renamed"]["created_at"] == 10.0
    assert steps[6]["statuses"]["renamed"]["kind"] == "stopped"
    assert "renamed" not in steps[-1]["threads"]
    restored = Registration(RegistryFile.path_in(tmp_path / "current"))
    assert restored.require("child").created_at == 20.0
    assert restored.snapshot().owner_generations["renamed"] > 0


def test_thread_rows_compare_unread_rows_as_stored_and_decode_only_what_changed(tmp_path):
    # A row that cannot decode proves an unread, unchanged row is never decoded to compare.
    stored = {'unread': {'name': 'unread', 'not': 'a thread'}}
    assert ThreadRows.from_wire(stored) == ThreadRows.from_wire(stored)
    written = ThreadRows.from_wire(stored)
    written['unread'] = ExternalThread(
        'unread', frozenset(), Checkout(tmp_path))
    other = ThreadRows.from_wire({})
    other['unread'] = written['unread']
    assert written == other
    assert written != ThreadRows.from_wire({})
