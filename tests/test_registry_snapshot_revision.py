"""A stored revision owns one immutable registry snapshot, including empty files."""

from agent_comms.repo import Checkout
from agent_comms.registration import Registration
from agent_comms.threads import Thread


def test_absence_and_recreation_have_distinct_snapshots(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    empty = registry.snapshot()
    assert registry.snapshot() is empty
    registry.register(Thread(name="worker", tags=frozenset(), worktree=Checkout(tmp_path)))
    populated = registry.snapshot()
    assert populated is not empty
    registry.store.path.unlink()
    absent = registry.snapshot()
    assert absent is not populated
    assert not absent.threads
    assert registry.snapshot() is absent
    registry.register(Thread(name="new", tags=frozenset(), worktree=Checkout(tmp_path)))
    assert set(registry.snapshot().threads) == {"new"}
    assert not absent.threads


def test_alias_names_remain_owned_by_their_original_snapshot(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread("worker", frozenset(), Checkout(tmp_path)))
    original = registry.snapshot()
    with registry.store.editing() as edit:
        edit.document.rename("worker", "middle")
        edit.commit()
    middle = registry.snapshot()
    assert middle.names_for("worker") == middle.names_for("middle") == frozenset({"worker", "middle"})
    with registry.store.editing() as edit:
        edit.document.rename("middle", "final")
        edit.commit()
    final = registry.snapshot()
    assert original.names_for("worker") == frozenset({"worker"})
    assert middle.names_for("middle") == frozenset({"worker", "middle"})
    assert final.names_for("worker") == final.names_for("final") == frozenset({"worker", "middle", "final"})
    assert final.names_for("unregistered") == frozenset({"unregistered"})
