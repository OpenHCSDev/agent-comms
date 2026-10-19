"""A stored revision owns one immutable registry snapshot, including empty files."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_comms.comms import Comms
from agent_comms.historical_views import ChannelDisplayHistory, DMDisplayHistory
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.store_files import _atomic_write_text


def test_snapshot_is_built_once_and_changes_only_with_the_revision(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registration(path)
    registry.register(registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    first = registry.snapshot()
    with patch.object(RegistrySnapshot, "capture", wraps=RegistrySnapshot.capture) as capture:
        for _ in range(20):
            assert Registration(path).snapshot() is first
        assert capture.call_count == 0
        with registry.store.editing() as edit:
            edit.document.last_seen["worker"] += 1
            assert edit.original.snapshot is first
            assert first.last_seen["worker"] != edit.document.last_seen["worker"]
            edit.commit()
        second = registry.snapshot()
        assert second is not first
        assert second.last_seen["worker"] == first.last_seen["worker"] + 1
        assert capture.call_count == 1
        raw = json.loads(path.read_text())
        raw["last_seen"]["worker"] += 1
        _atomic_write_text(path, json.dumps(raw))
        third = registry.snapshot()
        assert third is not second
        assert third.last_seen["worker"] == second.last_seen["worker"] + 1
        assert registry.snapshot() is third
        assert capture.call_count == 2


def test_concurrent_first_readers_share_one_snapshot(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registration(path)
    registry.register(registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    with patch.object(RegistrySnapshot, "capture", wraps=RegistrySnapshot.capture) as capture:
        with ThreadPoolExecutor(max_workers=4) as readers:
            snapshots = list(readers.map(lambda _: Registration(path).snapshot(), range(20)))
        assert all(snapshot is snapshots[0] for snapshot in snapshots)
        assert capture.call_count == 1


def test_channel_and_dm_revisions_use_the_cached_registry_snapshot(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread("worker", frozenset(), str(tmp_path)))
    comms.messaging.user_identity(str(tmp_path))
    snapshot = comms.registry.snapshot()
    with patch.object(RegistrySnapshot, "capture", side_effect=AssertionError("snapshot rebuilt")):
        channel = ChannelDisplayHistory.revision(comms.views, "#any", worktree=str(tmp_path))
        dm = DMDisplayHistory.revision(comms.views, "worker", worktree=str(tmp_path))
        assert channel.selection.viewer == dm.selection.viewer
        assert comms.registry.snapshot() is snapshot


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
