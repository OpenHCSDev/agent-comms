"""Ownership and protocol experiments for the Registration/A8 document boundary."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import LockedStore
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistryDocument, ThreadSlice
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import SharedLock
from agent_comms.threads import Thread


def owner(path, registry):
    return registry.new_thread("owner", frozenset(), str(path), process_identity=ProcessIdentity.capture(os.getpid()), created_at=10.0)


def test_edit_owns_turn_and_document_owns_later_changes(tmp_path, *, thread_registration):
    thread_registration.register(owner(tmp_path, registry=thread_registration))
    before = thread_registration.snapshot().owner_identity("owner")
    with thread_registration.store.editing() as edit:
        leased, generation = edit.lease_turn(edit.document.threads["owner"], "turn", None)
    assert generation == before.generation
    assert leased.turn_generation == 1
    thread_registration.release_turn(leased.turn_lease)
    document = thread_registration.store.read().copy()
    document.rename("owner", "renamed")
    document.unregister("renamed")
    document.begin_delete("renamed")
    assert document.remove("renamed") == ()
    assert document.threads == {}
    assert not list(tmp_path.iterdir())


def test_store_owns_document_and_failed_edit_cannot_leak_into_cache(tmp_path):
    registration = Registration(tmp_path / "registry.json")
    assert isinstance(registration.store, LockedStore)
    registration.register(owner(tmp_path, registry=registration))
    prior = registration.snapshot()
    raw = registration.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="aborted"), registration.store.editing() as edit:
        edit.document.unregister("owner")
        raise RuntimeError("aborted")
    assert registration.snapshot() == prior
    assert registration.store.path.read_bytes() == raw
    assert not {"_threads", "_owners", "_claims", "_statuses"} & vars(registration).keys()


def test_one_thread_entry_decodes_only_that_thread_and_matches_the_document(tmp_path):
    registration = Registration(tmp_path / "registry.json")
    registration.register(owner(tmp_path, registry=registration))
    registration.register(replace(owner(tmp_path, registry=registration), name="other", created_at=11.0))
    registration.rename("owner", "renamed")
    document = registration.store.read()
    raw = json.loads(registration.store.path.read_text())
    for name in ("renamed", "owner", "other"):
        assert RegistryDocument.entry_from_wire(raw, name) == document.entry(name)
    # Another process's write leaves this process without a decoded revision.
    registration.store.cache.entry = None
    read = registration.entry("owner")
    assert read.entry == document.entry("renamed") and read.entry.thread.name == "renamed"
    assert read.revision == registration.store.revision_unlocked()
    # Another thread's malformed declaration is not this thread's entry.
    raw["threads"]["other"]["created_at"] = "not a time"
    assert RegistryDocument.entry_from_wire(raw, "renamed") == document.entry("renamed")
    with pytest.raises(RelationViolationError):
        RegistryDocument.entry_from_wire(raw, "other")
    with pytest.raises(UnregisteredThreadError):
        RegistryDocument.entry_from_wire(raw, "missing")


def test_a_document_field_without_a_thread_slice_is_refused_at_definition():
    with pytest.raises(TypeError, match="Unsliced.rows declares no thread slice"):
        @ThreadSlice.required
        @dataclass
        class Unsliced:
            rows: dict = field(default_factory=dict)


def test_registry_update_persists_across_reopen(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(owner(tmp_path, registry=registry))

    def change(original):
        modified = original.copy()
        modified.threads["owner"] = replace(modified.threads["owner"], title="a8 update")
        return modified

    registry.store.update(change)
    assert Registration(registry.store.path).require("owner").title == "a8 update"


def test_new_thread_field_has_no_second_registry_decoder_roster(tmp_path, *, thread_registration):
    @dataclass(frozen=True, slots=True)
    class ExtendedThread(Thread):
        external_reference: str = "default"

    raw = FieldCodec.encode(owner(tmp_path, registry=thread_registration)) | {"external_reference": "declared here"}
    loaded = FieldCodec.decode(ExtendedThread, raw)
    assert loaded.external_reference == "declared here"
    assert loaded.to_wire()["external_reference"] == "declared here"


@pytest.mark.skipif(os.name != "posix", reason="nonblocking shared flock acceptance")
def test_registry_readers_share_lock_and_exclude_writer(tmp_path):
    from test_locked_store import assert_writer_excluded
    registry = Registration(tmp_path / "registry.json")
    registry.register(owner(tmp_path, registry=registry))
    other = RegistryStore(registry.store.path)
    with registry.store.reading():
        with other.locked(mode=SharedLock(), blocking=False):
            assert other._read_unlocked().threads["owner"].name == "owner"
        assert_writer_excluded(other.path)
    with other.locked(blocking=False):
        assert other._read_unlocked().threads["owner"].name == "owner"


def test_lifecycle_document_survives_fresh_process_restart(tmp_path):
    program = """
import json, os, sys
from dataclasses import replace
from pathlib import Path
from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.threads import Thread
from agent_comms.registration import Registration
from agent_comms.field_codec import FieldCodec
import time
time.time = lambda: 1000.0
root=Path(sys.argv[1]);root.mkdir()
r=Registration(root/'registry.json')
steps=[]
def capture():
    row = json.loads(r.store.path.read_text().replace(str(root), '<root>'))
    row['turns'] = {name: FieldCodec.encode(thread.turn_state.active)
                    for name, thread in r.snapshot().threads.items()}
    for turn in row['turns'].values():
        if turn is not None: turn['started_at'] = '<clock>'
    assert Registration(r.store.path).snapshot() == r.snapshot()
    steps.append(row)
r.register(r.new_thread('owner', frozenset({'team'}), str(root), process_identity=ProcessIdentity.capture(os.getpid()), created_at=10.0));capture()
r.register(r.new_thread('child', frozenset(), str(root), parent='owner', created_at=20.0));capture()
r.register(replace(r.require('owner'), title='metadata'));capture()
r.lease_local_turn('owner','first');capture()
r.release_turn(r.require('owner').turn_lease);capture()
r.rename('owner','renamed');capture()
r.unregister('renamed');capture()
r.archive('renamed');capture()
r.begin_delete('renamed');capture()
r.remove('renamed');capture()
print(json.dumps(steps,sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "current")],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src")),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    steps = json.loads(result.stdout)
    assert len(steps) == 10
    assert steps[2]["threads"]["owner"]["title"] == "metadata"
    assert steps[3]["turns"]["owner"]["id"] == "first"
    assert steps[4]["turns"]["owner"] is None
    assert steps[5]["threads"]["renamed"]["created_at"] == 10.0
    assert steps[6]["statuses"]["renamed"]["kind"] == "stopped"
    assert "renamed" not in steps[-1]["threads"]
    restored = Registration(tmp_path / "current" / "registry.json")
    assert restored.require("child").created_at == 20.0
    assert restored.snapshot().owner_generations["renamed"] > 0
