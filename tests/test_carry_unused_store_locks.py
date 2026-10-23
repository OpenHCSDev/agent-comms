"""A copy's lock beside a non-locking file goes; a declared writer fence and unknown locks stay."""

from agent_comms.installer.probes.carry import Carried
from agent_comms.native_session_files import NativeSessionLocks, PrivateSession
from agent_comms.store_files import store_lock_path
from cutover.carry_unused_store_locks import UnusedStoreLocks


def test_writer_fence_beside_a_session_survives_the_carry(tmp_path):
    session = PrivateSession.path_in(tmp_path, session_id="s1")
    session.parent.mkdir(parents=True)
    session.write_text("{}\n")
    fence = NativeSessionLocks(session).writer_lock
    leftover = store_lock_path(session)
    orphan = store_lock_path(tmp_path / "undeclared.json")
    for lock in (fence, leftover, orphan):
        lock.touch()

    step = UnusedStoreLocks(tmp_path, tmp_path, tmp_path / "spill")
    assert step.carry(apply=False) == Carried(1, {"deleted": 1, "kept": 1, "unknown": 1})
    step.carry(apply=True)

    assert fence.is_file() and orphan.is_file() and not leftover.exists()
    assert step.carry(apply=False).changed == 0
