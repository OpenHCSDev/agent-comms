"""A stored revision owns one immutable registry snapshot, including empty files."""

from agent_comms.registration import Registration


def test_absence_and_recreation_have_distinct_snapshots(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    empty = registry.snapshot()
    assert registry.snapshot() is empty
    registry.register(registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    populated = registry.snapshot()
    assert populated is not empty
    registry.store.path.unlink()
    absent = registry.snapshot()
    assert absent is not populated
    assert not absent.threads
    assert registry.snapshot() is absent
    registry.register(registry.new_thread(name="new", tags=frozenset(), worktree=str(tmp_path)))
    assert set(registry.snapshot().threads) == {"new"}
    assert not absent.threads
