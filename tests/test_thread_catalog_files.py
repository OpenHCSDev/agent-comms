"""A session directory declares its one catalog, and a lock is never itself locked."""

import pytest

from agent_comms.locked_store import RootFile
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
    with pytest.raises(ValueError, match="never itself a lockable store"):
        RootFile.copy_group(lock, tmp_path / "copy")
    assert not store_lock_path(lock).exists()


def test_lock_chains_are_deleted_and_store_locks_kept(tmp_path):
    lock = store_lock_path(_catalog(tmp_path))
    chain = [lock]
    for _ in range(3):
        chain.append(store_lock_path(chain[-1]))
    for path in chain:
        path.touch()
    step = LockChains(tmp_path, tmp_path, tmp_path / "spill")
    assert step.carry(apply=False).rewritten == ()
    carried = step.carry(apply=True)
    assert sorted(carried.rewritten) == sorted(path.relative_to(tmp_path) for path in chain[1:])
    assert lock.exists() and not any(path.exists() for path in chain[1:])
    assert step.carry(apply=False).changed == 0
