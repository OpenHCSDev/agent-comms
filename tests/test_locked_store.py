"""Canonical locks and durable bytes through a real declared document store."""
from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass, replace

import pytest

from agent_comms.locked_store import DocumentChange, LockedStore
from agent_comms.pi_counts import PiCount
from agent_comms.registry_store import RegistryStore
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoDocument, RuntimeInfoStore
from agent_comms.child_process import ChildLaunch, ChildRetirementFailed, DiesWithLauncherLifetime, Platform
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.runtime_children import RuntimeChildrenStore
from agent_comms.store_files import (
    ExclusiveLock,
    HolderNamingContention,
    StoreLockBusyError,
    StoreLockContention,
    UNBOUNDED_CONTENTION,
    _store_lock,
)


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
    with _store_lock(path, mode, UNBOUNDED_CONTENTION):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("lock holder was not released")


def _hold_store_write(owner, path, ready, release):
    with owner.write_guard(path):
        ready.set()
        if not release.wait(15):
            raise TimeoutError("store writer was not released")


def _guarded_read(owner, path, started, done, diagnostics):
    with diagnostics.open("w") as report, redirect_stderr(report):
        store = owner(path)
        started.set()
        with store.guarded_read():
            done.set()


def _record(path, start, prefix):
    if not start.wait(10):
        raise TimeoutError("writer was not started")
    for index in range(12):
        record(RuntimeInfoStore(path), observation(f"{prefix}-{index}"))


def _record_under_wire(path, ready):
    with _store_lock(path.parent / 'wire', ExclusiveLock(), UNBOUNDED_CONTENTION):
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
def test_registry_reads_wait_and_name_the_writer(tmp_path):
    """A multi-file registry cut waits and names its writer."""
    registry = RegistryStore(tmp_path / "registry.json")
    ready, release, started, done = event(), event(), event(), event()
    diagnostics = tmp_path / "registry-wait.log"
    with process(_hold_store_write, RegistryStore, registry.path, ready, release) as writer:
        try:
            assert ready.wait(5)
            with pytest.raises(StoreLockBusyError), registry.read_now():
                pytest.fail("a registry try-read entered during a write")
            with process(_guarded_read, RegistryStore, registry.path, started, done, diagnostics):
                assert started.wait(5)
                assert not done.wait(5.2), "a registry read crossed its held write lock"
                assert "store lock still waiting after 5 s, " in diagnostics.read_text()
                assert f"PID {writer.pid}: " in diagnostics.read_text()
                release.set()
                assert done.wait(5), "a registry read did not resume after its writer"
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


def test_slow_lock_wait_names_its_holder_once(tmp_path, capsys):
    """Install 18 waited 59 s for the registry lock and nobody could say who held it."""
    path = tmp_path / "held"
    path.write_bytes(b"")
    with open(path, "rb") as held, open(path, "rb") as waiting:
        fcntl.flock(held, fcntl.LOCK_EX)
        threading.Timer(.5, fcntl.flock, (held, fcntl.LOCK_UN)).start()
        HolderNamingContention(.1).acquire(
            waiting.fileno(), Platform.current(), mode=ExclusiveLock())
    err = capsys.readouterr().err
    assert err.count("store lock still waiting after 0.1 s, held by ") == 1, err
    assert f"PID {os.getpid()}: " in err


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
