"""A stored revision owns one immutable registry snapshot, including empty files."""

from agent_comms.repo import Checkout
from agent_comms.registration import Registration
from agent_comms.threads import Thread


def test_absence_and_recreation_have_distinct_snapshots(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    empty = registry.snapshot()
    assert registry.snapshot() is empty
    registry.register(Thread(name="worker", tags=frozenset(), worktree=Checkout.from_wire(str(tmp_path))))
    populated = registry.snapshot()
    assert populated is not empty
    registry.store.path.unlink()
    absent = registry.snapshot()
    assert absent is not populated
    assert not absent.threads
    assert registry.snapshot() is absent
    registry.register(Thread(name="new", tags=frozenset(), worktree=Checkout.from_wire(str(tmp_path))))
    assert set(registry.snapshot().threads) == {"new"}
    assert not absent.threads
