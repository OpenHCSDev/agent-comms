"""Canonical locks and durable bytes through a real declared document store."""
from __future__ import annotations

import asyncio
import fcntl
import json
import multiprocessing
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

import pytest

from agent_comms.locked_store import DocumentBytes, DocumentChange, LockedStore
from agent_comms.private_path import FileRevision, MissingFile
from agent_comms.coordination_errors import StaleRevision
from agent_comms.pi_counts import PiCount
from agent_comms.registry_store import RegistryStore
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoDocument, RuntimeInfoStore
from agent_comms.child_process import ChildLaunch, ChildRetirementFailed, DiesWithLauncherLifetime, Platform, StreamingChildStdio
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.runtime_children import RuntimeChildrenStore
from agent_comms.store_files import (
    ExclusiveLock,
    StoreLockBusyError,
    StoreLockContention,
    StoreGuard,
    _store_lock,
)


@pytest.mark.parametrize("existing", (False, True))
def test_document_bytes_captures_original_missing_or_existing_file(tmp_path, existing):
    path = tmp_path / "document" / "value.json"
    content = b'{"original":"bytes"}\n'
    if existing:
        path.parent.mkdir()
        path.write_bytes(content)
    captured = DocumentBytes.capture(path)
    if existing:
        assert captured == DocumentBytes(FileRevision.capture(path), content)
    else:
        assert captured == DocumentBytes(MissingFile(), b"")
        assert not path.exists() and not path.parent.exists()


def observation(thread="goal"):
    return AgentRuntimeInfo(thread=thread, timestamp=122.0, context_used=PiCount.from_wire(2))


def record(store, value):
    store.update(lambda document: DocumentChange.to(
        RuntimeInfoDocument({**document.observations, value.thread: value})
    ))


def clear(store, thread, *, timestamp=None):
    def change(document):
        current = document.observations.get(thread)
        if current is None or (timestamp is not None and current.timestamp != timestamp):
            return DocumentChange(document, False)
        return DocumentChange(RuntimeInfoDocument(
            {key: row for key, row in document.observations.items() if key != thread}
        ), True)

    return store.update(change)


def _hold_lock(path, mode, ready, release):
    with _store_lock(path, mode, StoreGuard.contention):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("lock holder was not released")


def _hold_store_write(owner, path, ready, release):
    with owner.write_guard(path):
        ready.set()
        if not release.wait(15):
            raise TimeoutError("store writer was not released")


def _record(path, start, prefix):
    if not start.wait(10):
        raise TimeoutError("writer was not started")
    for index in range(12):
        record(RuntimeInfoStore(path), observation(f"{prefix}-{index}"))


def _replace_captured_documents(root):
    RegistryStore(root / "registry.json").mark_historical()
    for name in ("first", "second"):
        record(RuntimeInfoStore(root / name), observation("successor"))


class RegistryWithConcurrentDecoder(RegistryStore, declared_name=None):
    """A real competing writer runs after capture and before any decoding."""

    def _decode_snapshot(self, source):
        with process(_replace_captured_documents, self.path.parent):
            pass
        return super()._decode_snapshot(source)



@dataclass(frozen=True, slots=True)
class RuntimeWithConcurrentDecoder(RuntimeInfoStore, declared_name=None):
    pending: list[str] = field(default_factory=lambda: ["concurrent"])

    def _decode_capture(self, source):
        if self.pending:
            name = self.pending.pop()
            ready = event()
            with process(_record, self.path, ready, name):
                ready.set()
        return super()._decode_capture(source)


class ImpatientRuntimeDecoder(RuntimeWithConcurrentDecoder, declared_name=None):
    contention = StoreLockContention(0)


def test_update_decodes_without_exclusion_and_never_applies_a_stale_change(tmp_path):
    path = tmp_path / "runtime_info.json"
    record(RuntimeInfoStore(path), observation("original"))
    store = RuntimeWithConcurrentDecoder(path)
    applied = []

    def change(document):
        applied.append(frozenset(document.observations))
        return DocumentChange.to(RuntimeInfoDocument({
            **document.observations, "local": observation("local"),
        }))

    store.update(change)
    expected = {"original", *(f"concurrent-{index}" for index in range(12))}
    assert applied == [frozenset(expected)]
    assert set(store.read().observations) == expected | {"local"}
    with pytest.raises(StaleRevision):
        ImpatientRuntimeDecoder(path).update(change)
    assert applied == [frozenset(expected)]


def _record_under_wire(path, ready):
    with _store_lock(path.parent / 'wire', ExclusiveLock(), StoreGuard.contention):
        store = RuntimeInfoStore(path)
        record(store, observation())
        assert store.read().observations == {"goal": observation()}
        assert clear(store, "goal", timestamp=122.0)
    ready.set()


@contextmanager
def process(target, *args):
    child = multiprocessing.get_context("spawn").Process(target=target, args=args)
    child.start()
    try:
        yield child
        child.join(10)
        assert not child.is_alive(), "bounded worker did not finish"
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(3)
        if child.is_alive():
            child.kill()
            child.join(3)
        child.close()


def event():
    return multiprocessing.get_context("spawn").Event()


def _writer_refused(path):
    with pytest.raises(BlockingIOError), _store_lock(path, ExclusiveLock(), StoreLockContention(0)):
        pytest.fail("writer entered during reader")


def assert_writer_excluded(path):
    """Probe another lock owner, not an illegal upgrade of the caller's read."""
    with process(_writer_refused, path):
        pass


def test_abstract_parent_and_real_adopter(tmp_path):
    @dataclass(frozen=True, slots=True)
    class AbstractDocument(LockedStore[RuntimeInfoDocument]):
        pass

    for owner in (LockedStore, AbstractDocument):
        with pytest.raises(TypeError):
            owner(tmp_path / "document")
    assert RuntimeInfoStore.read is LockedStore.read
    assert RuntimeInfoStore.update is LockedStore.update


def test_current_observation_persistence_and_noop(tmp_path):
    store = RuntimeInfoStore(tmp_path / "runtime_info.json")
    assert store.read() == RuntimeInfoDocument()
    assert not store.path.exists()
    assert not clear(store, "absent")
    assert not store.path.exists()
    record(store, observation())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.read().observations == {"goal": observation()}
    before = store.path.stat()
    assert not clear(store, "goal", timestamp=999.0)
    assert store.path.stat() == before
    assert clear(store, "goal", timestamp=122.0)
    assert store.read() == RuntimeInfoDocument()


def test_each_transition_is_visible_before_the_next_while_writer_stays_excluded(tmp_path):
    store = RuntimeInfoStore(tmp_path / "runtime_info.json")
    first = RuntimeInfoDocument({"first": observation("first")})
    second = RuntimeInfoDocument({**first.observations, "second": observation("second")})

    def changes(original):
        assert original == RuntimeInfoDocument()
        yield DocumentChange(first, "first")
        assert store.read() == first
        assert_writer_excluded(store.path)
        yield DocumentChange(second, "second")

    assert store.update_each(changes) == ("first", "second")
    assert store.read() == second


def test_related_snapshots_keep_their_cut_and_release_writers_before_decode(tmp_path):
    first = RuntimeInfoStore(tmp_path / "first")
    second = RuntimeInfoStore(tmp_path / "second")
    for store in (first, second):
        record(store, observation("original"))
    registry = RegistryWithConcurrentDecoder(tmp_path / "registry.json")
    snapshot, left, right = registry.snapshot_with_pair(first, second)
    snapshot.purpose.require_write()
    assert left.observations == right.observations == {"original": observation("original")}
    assert set(first.read().observations) == set(second.read().observations) == {
        "original", "successor",
    }


def test_mode_preserved_and_new_inode_published(tmp_path):
    store = RuntimeInfoStore(tmp_path / "runtime_info.json")
    record(store, observation())
    store.path.chmod(0o640)
    inode = store.path.stat().st_ino
    record(store, replace(observation(), context_used=PiCount.from_wire(3)))
    assert store.path.stat().st_ino != inode
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o640
    assert store.read().observations["goal"].context_used == PiCount.from_wire(3)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".runtime_info.json.lock", "runtime_info.json",
    ]


def test_unknown_observation_fields_are_rejected_without_rewriting_saved_data(tmp_path):
    store = RuntimeInfoStore(tmp_path / "runtime_info.json")
    data = RuntimeInfoDocument({"goal": observation()}).to_wire()
    data["goal"]["future_field"] = {"not_authoritative_here": True}
    store.path.write_text(json.dumps(data))
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.read()
    with pytest.raises(ValueError):
        record(store, observation())
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stage", ["file_sync", "replace", "directory_sync"])
def test_write_failure_preserves_previous_bytes(tmp_path, monkeypatch, existing, stage):
    store = RuntimeInfoStore(tmp_path / "runtime_info.json")
    if existing:
        record(store, observation())
        store.path.chmod(0o640)
    original_fsync = os.fsync
    original_replace = os.replace
    failed = False

    def fsync(fd):
        nonlocal failed
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if not failed and (
            (stage == "file_sync" and not directory) or (stage == "directory_sync" and directory)
        ):
            failed = True
            raise OSError("injected sync failure")
        return original_fsync(fd)

    def publish(source, target):
        if stage == "replace":
            raise OSError("injected replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", publish)
    with pytest.raises(OSError):
        record(store, replace(observation(), context_used=PiCount.from_wire(999)))
    if existing:
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o640
    else:
        assert not store.path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.previous"))


def test_callback_and_encoding_fail_before_publication(tmp_path):
    store = RuntimeInfoStore(tmp_path / "runtime_info.json")
    record(store, observation())

    def fail(document):
        raise RuntimeError("change failed")

    with pytest.raises(RuntimeError):
        store.update(fail)
    with pytest.raises(ValueError):
        replace(observation(), timestamp=float("nan"))
    original = store.path.read_bytes()
    invalid = observation()
    object.__setattr__(invalid, "timestamp", float("nan"))
    with pytest.raises(TypeError):
        record(store, invalid)
    assert store.path.read_bytes() == original
    record(store, replace(observation(), context_used=PiCount.from_wire(3)))


def test_invalid_boundary_is_not_overwritten(tmp_path):
    store = RuntimeInfoStore(tmp_path / "runtime_info.json")
    data = RuntimeInfoDocument({"goal": observation()}).to_wire()
    data["goal"]["context_used"] = True
    text = json.dumps(data)
    store.path.write_text(text)
    with pytest.raises(ValueError):
        record(store, observation())
    assert store.path.read_text() == text


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock contention contract")
def test_registry_wait_expires_and_names_the_writer(tmp_path):
    """The multi-file guard refuses a held writer at its declared bound."""
    registry = RegistryStore(tmp_path / "registry.json")
    ready, release = event(), event()
    with process(_hold_store_write, RegistryStore, registry.path, ready, release) as writer:
        try:
            assert ready.wait(5)
            with pytest.raises(StoreLockBusyError), registry.read_now():
                pytest.fail("a registry try-read entered during a write")
            with pytest.raises(StoreLockBusyError) as refused, registry.guarded_write():
                pytest.fail("a writer crossed the held lock")
            assert f"PID {writer.pid}: " in str(refused.value)
        finally:
            release.set()


def test_concurrent_writers_keep_all_rows(tmp_path):
    path, start = tmp_path / "runtime_info.json", event()
    with process(_record, path, start, "first"), process(_record, path, start, "second"):
        start.set()
    assert set(RuntimeInfoStore(path).read().observations) == {
        f"{prefix}-{index}" for prefix in ("first", "second") for index in range(12)
    }


def test_wire_nesting_does_not_reacquire_document_lock(tmp_path):
    done = event()
    with process(_record_under_wire, tmp_path / "runtime_info.json", done):
        assert done.wait(5)


def test_declared_wait_is_not_spent_by_another_acquisition(tmp_path):
    path = tmp_path / "held"
    path.write_bytes(b"")
    policy = StoreLockContention(.01)
    with open(path, "rb") as held, open(path, "rb") as waiting:
        fcntl.flock(held, fcntl.LOCK_EX)
        for _ in range(2):
            with pytest.raises(StoreLockBusyError) as refused:
                policy.acquire(waiting.fileno(), Platform.current(), mode=ExclusiveLock())
            assert f"PID {os.getpid()}: " in str(refused.value)
            assert policy.seconds == .01


@pytest.mark.asyncio
async def test_cancelled_spawn_joins_record_and_retires_its_registered_child(tmp_path):
    store = RuntimeChildrenStore.at(tmp_path)
    ready, release = event(), event()
    marker = tmp_path / "executed"
    with process(_hold_store_write, RuntimeChildrenStore, store.path, ready, release):
        assert ready.wait(5)
        with ChildLaunch(
            Platform.current(),
            (sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)),
            (), LaunchRuntime.at_prefix(tmp_path), DiesWithLauncherLifetime,
        ) as launch:
            pending = asyncio.create_task(launch.spawn_async(
                cwd=tmp_path, env=os.environ, stdio=StreamingChildStdio(input_enabled=False),
            ))
            try:
                async with asyncio.timeout(5):
                    # Closing the parent's identity writer happens after spawn,
                    # immediately before the join of the blocked record writer.
                    while not launch.identity_writer.closed:
                        await asyncio.sleep(0)
                    pending.cancel()
                    await asyncio.sleep(0)
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
    assert store.read().records == ()
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runtime_retirement_refuses_and_reaps_an_unrecorded_exec_gated_child(tmp_path):
    store = RuntimeChildrenStore.at(tmp_path)
    store.read().require_open()
    marker = tmp_path / "executed"
    with ChildLaunch(
        Platform.current(),
        (sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)),
        (), LaunchRuntime.running(), DiesWithLauncherLifetime,
    ) as launch:
        process = subprocess.Popen(launch.argv, **launch.options)
        child = launch.spawned(process, store)
        try:
            RuntimeChildrenStore.retire(tmp_path)
            with pytest.raises(ChildRetirementFailed):
                await launch.record_async(child.identity, store)
            assert process.wait(timeout=2) < 0
            assert not child.alive()
            assert not marker.exists()
            assert store.read().records == ()
        finally:
            launch.cancel_before_release(child.identity)
            child.reap()
            child.close_streams()
