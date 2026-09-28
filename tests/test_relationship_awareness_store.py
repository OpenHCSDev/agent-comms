"""A8 adopter contracts: exact formats, optional corruption, locks and durability."""

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import Comms, Thread, ThreadSort, locked_store
from agent_comms.declarations import _store_lock
from agent_comms.locked_store import LockedStore
from agent_comms.passive_channel_awareness import (
    PassiveAwarenessDocument,
    PassiveAwarenessRecord,
    PassiveAwarenessStore,
    PassiveChannelAwareness,
)
from agent_comms.relationships import RelationshipDocument, RelationshipStore
from test_locked_store import _hold_lock, event, process
from test_relationships import setup_wire


def owner():
    return Thread("owner", frozenset({"team"}), "/fixture", created_at=12.0)


def initialize(awareness, thread=None):
    awareness.initialize(
        thread or owner(), admission=1, high_water=7, channels=frozenset({"#team"}), fresh=False
    )


def _read_adopter(root, kind, done):
    if kind == "relationships":
        service = Comms(root).relationships
        service.collaborations("owner")
        service.contact_projection("owner")
        service.snapshot("owner")
    else:
        PassiveChannelAwareness(root).sources(owner())
    done.set()


def _initialize_owner(root, number, start):
    if not start.wait(10):
        raise TimeoutError("writer never released")
    initialize(
        PassiveChannelAwareness(root), replace(owner(), name=str(number), created_at=float(number))
    )


def test_golden_relationship_document_and_noop(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    service = comms.relationships
    assert service.store.read() == RelationshipDocument()
    assert not service.store.path.exists()
    monkeypatch.setattr("agent_comms.relationships.time.time", lambda: 123.0)
    first, peer = comms.registry.require("owner"), comms.registry.require("peer")
    service.edit("owner", "add", "peer", "Review")
    service.set_order("owner", "children", ThreadSort.CREATED)
    expected = {
        "collaborations": [
            {
                "owner": "owner",
                "peer": "peer",
                "owner_created": first.created_at,
                "peer_created": peer.created_at,
                "note": "Review",
                "created_at": 123.0,
                "updated_at": 123.0,
                "history": [],
            }
        ],
        "orders": [
            {
                "owner": "owner",
                "owner_created": first.created_at,
                "group": "children",
                "order": "created_at",
            }
        ],
    }
    expected["version"] = 2
    assert service.store.path.read_text() == json.dumps(expected, indent=2) + "\n"
    before = service.store.path.stat()
    service.edit("owner", "add", "peer", "ignored")
    service.edit("owner", "remove", "absent")
    assert service.store.path.stat() == before
    assert RelationshipStore.read is LockedStore.read
    assert RelationshipStore.update is LockedStore.update


def test_golden_passive_scope_and_typed_records(tmp_path):
    awareness = PassiveChannelAwareness(tmp_path)
    assert awareness.sources(owner()) == ()
    assert not awareness.store.path.exists()
    initialize(awareness)
    key = PassiveAwarenessRecord.key(owner().created_at)
    row = {
        "name": "owner",
        "created_at": 12.0,
        "admission": 1,
        "cursor": 7,
        "scope_after": 7,
        "scope_generation": 0,
        "channels": ["#team"],
        "known": [],
    }
    expected = {"version": 1, "rows": {key: row}}
    assert awareness.store.path.read_text() == json.dumps(expected, sort_keys=True)
    before = awareness.store.path.stat()
    initialize(awareness)
    assert awareness.store.path.stat() == before
    row["known"] = [[8, "#team", "a" * 64]]
    awareness.store.path.write_text(json.dumps(expected))
    assert awareness.sources(owner()) == ((8, "#team", "a" * 64),)
    changed = replace(owner(), channel_scope_generation=2)
    awareness.scope_changed(changed, admission=1, high_water=9, channels=frozenset({"#next"}))
    stored = json.loads(awareness.store.path.read_text())["rows"][key]
    assert stored == {
        **row,
        "scope_after": 9,
        "scope_generation": 2,
        "channels": ["#next"],
        "known": [],
    }
    assert PassiveAwarenessStore.update is LockedStore.update
    assert PassiveAwarenessStore.read is LockedStore.read


@pytest.mark.parametrize(
    "text", ["{", "[]", '{"version": 2}', '{"version": 1, "rows": []}', "\udcff"]
)
def test_corrupt_passive_file_is_never_replaced(tmp_path, text):
    awareness = PassiveChannelAwareness(tmp_path)
    before = text.encode("utf-8", errors="surrogateescape")
    awareness.store.path.write_bytes(before)
    assert awareness.sources(owner()) == ()
    initialize(awareness)
    awareness.scope_changed(owner(), admission=1, high_water=10, channels=frozenset())
    assert awareness.store.path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("created_at", 12),
        ("admission", True),
        ("unrecognized_data", "not erased"),
        ("cursor", -1),
        ("scope_after", 6),
        ("scope_generation", -1),
        ("channels", [1]),
        ("known", [[7, "#team", "a" * 64]]),
        ("known", [[8, "peer", "a" * 64]]),
        ("known", [[8, "#team", "short"]]),
        ("known", [[8, "#team", "a" * 64]] * 17),
    ],
)
def test_corrupt_passive_rows_fail_closed(tmp_path, field, value):
    awareness = PassiveChannelAwareness(tmp_path)
    initialize(awareness)
    data = json.loads(awareness.store.path.read_text())
    data["rows"][PassiveAwarenessRecord.key(owner().created_at)][field] = value
    before = json.dumps(data)
    awareness.store.path.write_text(before)
    initialize(awareness)
    assert awareness.sources(owner()) == ()
    assert awareness.store.path.read_text() == before


@pytest.mark.parametrize("text", ["{", "[]", '{"version": 2}'])
def test_relationship_corruption_stays_strict(tmp_path, text):
    comms = setup_wire(tmp_path)
    comms.relationships.store.path.write_text(text)
    with pytest.raises(ValueError):
        comms.relationships.collaborations("owner")
    with pytest.raises(ValueError):
        comms.relationships.edit("owner", "add", "peer")
    assert comms.relationships.store.path.read_text() == text


def test_unreadable_policy_does_not_hide_callback_errors(tmp_path, monkeypatch):
    strict, optional = RelationshipStore(tmp_path / "r"), PassiveAwarenessStore(tmp_path / "p")

    def fail_read(*_args, **_kwargs):
        raise PermissionError("read denied")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_text", fail_read)
        assert optional.read() is None
        with pytest.raises(PermissionError):
            strict.read()

    def fail_change(_document):
        raise ValueError("callback failed")

    with pytest.raises(ValueError, match="callback failed"):
        optional.update(fail_change)


@pytest.mark.skipif(os.name != "posix", reason="POSIX lock contention")
@pytest.mark.parametrize("kind", ["relationships", "passive"])
def test_actual_adopter_reads_share_document_lock(tmp_path, kind):
    comms = setup_wire(tmp_path)
    path = (
        comms.relationships.store.path
        if kind == "relationships"
        else PassiveChannelAwareness(comms.root).store.path
    )
    ready, release, done = event(), event(), event()
    with process(_hold_lock, path, True, ready, release):
        try:
            assert ready.wait(5)
            with process(_read_adopter, comms.root, kind, done):
                assert done.wait(5), "adopter read requested an exclusive document lock"
            with pytest.raises(BlockingIOError), _store_lock(path, blocking=False):
                pass
        finally:
            release.set()


def test_shared_read_scope_holds_lock_until_exit(tmp_path):
    store = PassiveAwarenessStore(tmp_path / "p")
    with store.reading() as document:
        assert document == PassiveAwarenessDocument()
        # Separate process descriptors would see the same conflict; this separate
        # descriptor also proves reading() has not already released its lock.
        with pytest.raises(BlockingIOError), _store_lock(store.path, blocking=False):
            pass
    with _store_lock(store.path, blocking=False):
        pass


def test_concurrent_passive_initializations_preserve_every_owner(tmp_path):
    start = event()
    with (
        process(_initialize_owner, tmp_path, 11, start),
        process(_initialize_owner, tmp_path, 22, start),
    ):
        start.set()
    data = PassiveAwarenessStore(tmp_path / PassiveAwarenessStore.filename).read()
    assert {row.name for row in data.rows.values()} == {"11", "22"}


@pytest.mark.parametrize("kind", ["relationships", "passive"])
@pytest.mark.parametrize("stage", ["file_sync", "replace", "directory_sync"])
def test_adopter_failure_restores_old_bytes_and_mode(tmp_path, monkeypatch, kind, stage):
    comms = setup_wire(tmp_path)
    awareness = PassiveChannelAwareness(comms.root)
    if kind == "relationships":
        comms.relationships.edit("owner", "add", "peer", "old")
        path = comms.relationships.store.path

        def mutate():
            comms.relationships.edit("owner", "update", "peer", "new")

    else:
        initialize(awareness)
        path = awareness.store.path

        def mutate():
            awareness.scope_changed(owner(), admission=1, high_water=99, channels=frozenset())

    path.chmod(0o640)
    before = path.read_bytes()
    real_sync, real_replace = os.fsync, locked_store._replace_snapshot
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
    monkeypatch.setattr(locked_store, "_replace_snapshot", publish)
    with pytest.raises(OSError, match="injected"):
        mutate()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not list(path.parent.glob(".*.tmp"))
    assert not list(path.parent.glob(".*.previous"))
