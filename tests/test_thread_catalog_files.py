"""A session directory declares its one catalog, and a lock is never itself locked."""

import pytest

from agent_comms.locked_store import LockedFile, RootFile
from agent_comms.read_ledger import ReadLedger
from agent_comms.store_files import store_lock_path
from agent_comms.threads import ThreadCatalog
from cutover.carry_lock_chains import LockChains


def _catalog(root):
    catalog = ThreadCatalog.path_in(root, lookup="0" * 32)
    catalog.parent.mkdir(parents=True)
    catalog.write_text("{}")
    return catalog


def test_session_locks_are_undeclared_and_never_locked(tmp_path):
    catalog = _catalog(tmp_path)
    lock = store_lock_path(catalog)
    lock.touch()
    assert ThreadCatalog.matching_in(tmp_path) == frozenset((catalog,))
    RootFile.copy_group(lock, tmp_path / "copy")
    with pytest.raises(ValueError, match="never itself a lockable store"):
        LockedFile.copy_group(lock, tmp_path / "locked-copy")
    assert not store_lock_path(lock).exists()


def test_copy_locks_only_files_their_writers_lock(tmp_path):
    """A copy of a root creates a store lock only beside a store whose writers take it."""
    root, copy = tmp_path / "root", tmp_path / "copy"
    ledger = root / "imported_sessions" / "selected-tool-ledger"
    ledger.mkdir(parents=True)
    plain = tuple(ledger / f"slot-{index}" for index in range(3))
    for path in plain:
        path.write_text("{}")
    store = ReadLedger.path_in(root)
    store.write_text("{}")
    for resource, owner in RootFile.owned_files(root, (*plain, store)).items():
        target = copy / resource.path
        target.parent.mkdir(parents=True, exist_ok=True)
        owner.copy_group(root / resource.path, target)
    assert not any(ledger.glob(".*.lock"))
    assert store_lock_path(store).exists()
    assert sorted(path.name for path in (copy / ledger.relative_to(root)).iterdir()) == [
        path.name for path in plain]


def test_lock_chains_are_deleted_and_store_locks_kept(tmp_path):
    lock = store_lock_path(_catalog(tmp_path))
    chain = [lock]
    for _ in range(3):
        chain.append(store_lock_path(chain[-1]))
    for path in chain:
        path.touch()
    step = LockChains(tmp_path, tmp_path, tmp_path / "spill")
    step.carry(apply=True)
    assert lock.exists() and not any(path.exists() for path in chain[1:])
    assert step.carry(apply=False).changed == 0
