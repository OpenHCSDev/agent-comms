"""Relationship owner contracts: corruption, locks and durability."""


import os
import stat

import pytest

from agent_comms.relationships import RelationshipEdit
from agent_comms.comms import wire
from agent_comms.relationships import RelationshipDocument, RelationshipStore
from agent_comms.store_files import SharedLock, _store_lock
from test_locked_store import _hold_lock, assert_writer_excluded, event, process
from test_relationships import setup_wire


def _read_adopter(root, done):
    service = wire(root).relationships
    service.collaborations("owner")
    service.contacts("owner")
    service.snapshot("owner")
    done.set()


@pytest.mark.parametrize("text", ["{", "[]", '{"version": 2}'])
def test_relationship_corruption_stays_strict(tmp_path, text):
    comms = setup_wire(tmp_path)
    comms.relationships.store.path.write_text(text)
    with pytest.raises(ValueError):
        comms.relationships.collaborations("owner")
    with pytest.raises(ValueError):
        comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer")
    assert comms.relationships.store.path.read_text() == text


@pytest.mark.skipif(os.name != "posix", reason="POSIX lock contention")
def test_actual_adopter_reads_share_document_lock(tmp_path):
    comms = setup_wire(tmp_path)
    path = comms.relationships.store.path
    ready, release, done = event(), event(), event()
    with process(_hold_lock, path, SharedLock(), ready, release):
        try:
            assert ready.wait(5)
            with process(_read_adopter, comms.root, done):
                assert done.wait(5), "adopter read requested an exclusive document lock"
            with pytest.raises(BlockingIOError), _store_lock(path, blocking=False):
                pass
        finally:
            release.set()


def test_shared_read_scope_holds_lock_until_exit(tmp_path):
    store = RelationshipStore(tmp_path / "r")
    with store.reading() as document:
        assert document == RelationshipDocument()
        assert_writer_excluded(store.path)
    with _store_lock(store.path, blocking=False):
        pass


@pytest.mark.parametrize("stage", ["file_sync", "replace", "directory_sync"])
def test_adopter_failure_restores_old_bytes_and_mode(tmp_path, monkeypatch, stage):
    comms = setup_wire(tmp_path)
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "old")
    path = comms.relationships.store.path

    def mutate():
        comms.relationships.edit("owner", RelationshipEdit.member_named("update"), "peer", "new")

    path.chmod(0o640)
    before = path.read_bytes()
    real_sync, real_replace = os.fsync, os.replace
    failed = False

    def sync(fd):
        nonlocal failed
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if not failed and (
            (stage == "directory_sync" and directory) or (stage == "file_sync" and not directory)
        ):
            failed = True
            raise OSError("injected sync")
        return real_sync(fd)

    def publish(source, target):
        if stage == "replace":
            raise OSError("injected replace")
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", publish)
    with pytest.raises(OSError):
        mutate()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not list(path.parent.glob(".*.tmp"))
    assert not list(path.parent.glob(".*.previous"))
