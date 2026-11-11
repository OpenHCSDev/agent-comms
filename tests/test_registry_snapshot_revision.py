"""A stored revision owns one immutable registry snapshot, including empty files."""

from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile


def test_absence_and_recreation_have_distinct_snapshots(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    empty = registry.snapshot()
    assert registry.snapshot() is empty
    registry.register(NativeThread(name="worker", tags=frozenset(), worktree=Checkout(tmp_path)))
    populated = registry.snapshot()
    assert populated is not empty
    registry.store.path.unlink()
    absent = registry.snapshot()
    assert absent is not populated
    assert not absent.threads
    assert registry.snapshot() is absent
    registry.register(NativeThread(name="new", tags=frozenset(), worktree=Checkout(tmp_path)))
    assert set(registry.snapshot().threads) == {"new"}
    assert not absent.threads
