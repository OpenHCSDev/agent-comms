"""A session directory declares its one catalog, and a lock is never itself locked."""

from pathlib import Path

import pytest

from agent_comms.messaging.read_ledger import ReadLedger
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.threads import ThreadCatalogFile
from agent_comms.storage.locked_store import (
    InstallationFiles,
    LiveFilesRequest,
    LockedFile,
    RootFile,
)
from agent_comms.storage.store_files import store_lock_path


def _catalog(root):
    catalog = ThreadCatalogFile.path_in(root, lookup="0" * 32)
    catalog.parent.mkdir(parents=True)
    catalog.write_text("{}")
    return catalog


def test_session_locks_are_undeclared_and_never_locked(tmp_path):
    catalog = _catalog(tmp_path)
    lock = store_lock_path(catalog)
    lock.touch()
    assert ThreadCatalogFile.matching_in(tmp_path) == frozenset((catalog,))
    RootFile.copy_group(lock, tmp_path / "copy")
    with pytest.raises(ValueError, match="never itself a lockable store"):
        LockedFile.copy_group(lock, tmp_path / "locked-copy")
    assert not store_lock_path(lock).exists()


@pytest.mark.parametrize("store_type,copy_takes_lock", [
    pytest.param(ReadLedger, False, id="atomic-single-file"),
    pytest.param(RegistryStore, True, id="multi-file-registry"),
])
def test_copy_uses_the_declared_read_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    store_type: type[ReadLedger] | type[RegistryStore], copy_takes_lock: bool,
):
    """Atomic documents copy without read locks; the registry copies under its shared guard."""
    root, copy = tmp_path / "root", tmp_path / "copy"
    directory = root / "plain-files"
    directory.mkdir(parents=True)
    plain = tuple(directory / f"slot-{index}" for index in range(3))
    for path in plain:
        path.write_text("{}")
    store = store_type.at(root)
    document = store.empty()
    store.replace(document)
    guarded_reads = []
    guarded_read = store_type.guarded_read

    def reading(selected):
        guarded_reads.append(selected.path)
        return guarded_read(selected)

    monkeypatch.setattr(store_type, "guarded_read", reading)
    for resource in InstallationFiles(LiveFilesRequest(root)).owned_files((*plain, store.path)):
        target = copy / resource.path
        target.parent.mkdir(parents=True, exist_ok=True)
        resource.owner.copy_group(resource.live(root), target)
    assert not any(directory.glob(".*.lock"))
    assert (store.path in guarded_reads) == copy_takes_lock
    assert store.read() == document
    assert store_type.at(copy).read() == document
    assert sorted(path.name for path in (copy / directory.relative_to(root)).iterdir()) == [
        path.name for path in plain]
    for path in plain:
        assert (copy / path.relative_to(root)).read_bytes() == path.read_bytes()


