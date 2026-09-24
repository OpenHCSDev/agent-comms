"""Windows must fail closed rather than run a store without directory durability."""

import os

import pytest

from agent_comms.goal_attempts import GoalAttemptStore, StorageUncertain


@pytest.mark.skipif(os.name == "posix", reason="Windows-only fail-closed contract")
def test_unsupported_windows_goal_store_never_creates_or_opens(tmp_path):
    root = tmp_path / "owner"
    root.mkdir()
    with pytest.raises(StorageUncertain, match="POSIX owner-only directory fsync"):
        GoalAttemptStore.initialize(root)
    assert not (root / "goal_attempts.sqlite3").exists()
    with pytest.raises(StorageUncertain, match="POSIX owner-only directory fsync"):
        GoalAttemptStore(root)
