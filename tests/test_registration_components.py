"""Ownership and protocol experiments for the Registration/A8 document boundary."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.declarations import Thread, ThreadStatus
from agent_comms.locked_store import LockedStore
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore


def owner(path):
    return Thread("owner", frozenset(), str(path), pid=os.getpid(), created_at=10.0)


def test_document_owns_lifecycle_without_registration_or_io(tmp_path):
    document = RegistryDocument()
    document.apply_registration(
        document.prepare_registration(owner(tmp_path), ThreadStatus.RUNNING, new_owner=False)
    )
    before = document.snapshot().owner_identity("owner")
    claimed, generation = document.claim_turn(document.threads["owner"], "turn", None)
    assert generation == before.generation
    assert claimed.turn_generation == 1
    document.finish_claimed_turn_with_fence("owner", "turn")
    document.rename("owner", "renamed")
    document.unregister("renamed")
    document.begin_delete("renamed")
    assert document.remove("renamed") == ()
    assert document.threads == {}
    assert not list(tmp_path.iterdir())


def test_store_owns_document_and_failed_edit_cannot_leak_into_cache(tmp_path):
    registration = Registration(tmp_path / "registry.json")
    assert isinstance(registration.store, LockedStore)
    registration.register(owner(tmp_path))
    prior = registration.snapshot()
    raw = registration.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="aborted"), registration.store.editing() as edit:
        edit.document.unregister("owner")
        raise RuntimeError("aborted")
    assert registration.snapshot() == prior
    assert registration.store.path.read_bytes() == raw
    assert not {"_threads", "_owners", "_admissions", "_statuses"} & vars(registration).keys()


def test_registry_a8_update_uses_existing_wire_projection(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(owner(tmp_path))

    def change(original):
        modified = original.copy()
        modified.threads["owner"] = replace(modified.threads["owner"], title="a8 update")
        return modified

    registry.store.update(change)
    raw = json.loads(registry.store.path.read_text())
    assert raw["threads"]["owner"]["title"] == "a8 update"
    assert "owner_epochs" in raw and "owners" not in raw
    assert Registration(registry.store.path).require("owner").title == "a8 update"


def test_new_thread_field_has_no_second_registry_decoder_roster(tmp_path):
    @dataclass(frozen=True, slots=True)
    class ExtendedThread(Thread):
        external_reference: str = "default"

    raw = json.loads(json.dumps(owner(tmp_path).to_wire())) | {
        "external_reference": "declared here"
    }
    loaded = ExtendedThread.from_registry("owner", raw, tmp_path)
    assert loaded.external_reference == "declared here"
    assert loaded.to_wire()["external_reference"] == "declared here"


@pytest.mark.skipif(os.name != "posix", reason="nonblocking shared flock acceptance")
def test_registry_readers_share_lock_and_exclude_writer(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(owner(tmp_path))
    other = RegistryStore(registry.store.path)
    with registry.store.reading():
        with other.locked(shared=True, blocking=False):
            assert other._read_unlocked().threads["owner"].name == "owner"
        with pytest.raises(BlockingIOError), other.locked(blocking=False):
            pytest.fail("writer entered during reader")
    with other.locked(blocking=False):
        assert other._read_unlocked().threads["owner"].name == "owner"


def test_lifecycle_document_survives_fresh_process_restart(tmp_path):
    program = """
import json, os, sys
from dataclasses import replace
from pathlib import Path
from agent_comms.declarations import Thread, ThreadStatus
from agent_comms.registration import Registration
import time
time.time = lambda: 1000.0
os.getpid = lambda: 54321
root=Path(sys.argv[1]);root.mkdir()
r=Registration(root/'registry.json')
steps=[]
def capture():
    row = json.loads(r.store.path.read_text().replace(str(root), '<root>'))
    for thread in row['threads'].values():
        if thread.get('active_turn'): thread['active_turn']['started_at'] = '<clock>'
    assert Registration(r.store.path).snapshot() == r.snapshot()
    steps.append(row)
r.register(Thread('owner', frozenset({'team'}), str(root), pid=54321, created_at=10.0));capture()
r.register(Thread('child', frozenset(), str(root), parent='owner', created_at=20.0));capture()
r.register(replace(r.require('owner'), title='metadata'));capture()
r.claim_local_turn('owner','first');capture()
r.finish_claimed_turn('owner','first');capture()
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
    assert steps[3]["threads"]["owner"]["active_turn"]["id"] == "first"
    assert steps[4]["threads"]["owner"]["active_turn"] is None
    assert steps[5]["threads"]["renamed"]["created_at"] == 10.0
    assert steps[6]["threads"]["renamed"]["status"] == "stopped"
    assert "renamed" not in steps[-1]["threads"]
    restored = Registration(tmp_path / "current" / "registry.json")
    assert restored.require("child").created_at == 20.0
    assert restored.snapshot().owner_generations["renamed"] > 0
