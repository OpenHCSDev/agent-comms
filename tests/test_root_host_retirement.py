"""An install's target retires the root's host before any carry lets it read the stored registry:
on 2026-11-04 that read stopped the live install, after the outgoing runtime had already retired
the host. Only a running host needs its owners read."""

import os
from pathlib import Path

import pytest

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.root_runtime import (
    NoRootHost,
    RecordedRootHost,
    RootRuntime,
    RootRuntimeStore,
)
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.recorded_bus import RecordedBus
from agent_comms.registry.registry_document import InvalidRegistryDocument
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.storage.private_path import FileIdentity


def unreadable_registry(root: Path) -> None:
    """Corrupt the current document: a retired host must not try to decode it."""
    RegistryStore.at(root).rewrite_stored(lambda raw: {**raw, "version": None})


def store_with(root: Path, process: ProcessIdentity) -> RootRuntimeStore:
    bus = Bus.path_in(root)
    bus.parent.mkdir(parents=True, exist_ok=True)
    bus.touch()
    store = RootRuntimeStore.at(root)
    store.replace(RootRuntime(root / "prefix", RecordedRootHost(
        process, RecordedBus(FileIdentity.from_stat(bus.stat())))))
    return store


def test_a_retired_host_needs_no_registry(tmp_path):
    unreadable_registry(tmp_path)
    store = RootRuntimeStore.at(tmp_path)
    store.replace(RootRuntime(tmp_path / "prefix"))
    store.retire_host().require_retired(tmp_path)
    assert store.read().host == NoRootHost()


def test_a_dead_recorded_host_needs_no_registry(tmp_path):
    unreadable_registry(tmp_path)
    store_with(tmp_path, ProcessIdentity(os.getpid(), 1)).retire_host().require_retired(tmp_path)


def test_a_running_host_still_answers_to_its_owners(tmp_path):
    """Refused loudly and left running: an unreadable registry never stops a live host."""
    unreadable_registry(tmp_path)
    running = ProcessIdentity.capture(os.getpid())
    store = store_with(tmp_path, running)
    with pytest.raises(InvalidRegistryDocument):
        store.retire_host()
    assert store.read().host.observed(tmp_path).alive()
