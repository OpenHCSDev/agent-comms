from dataclasses import replace
import sqlite3

from agent_comms.nominal.field_codec import GoalId
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import GoalWaits, GoalWaitTargetRow, RecordedGoalWait
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.typed_table import ReconciledFile, WriterOwnedFile
from test_goal_standby_liveness import _finish, _waiting


def test_schema_carry_rederives_target_index_without_changing_saved_waits(tmp_path):
    comms, goal = _waiting(tmp_path)
    waits = comms.goals.waits
    original = waits.read()[goal.id]
    target = original.targets[0].incarnation
    orphan = GoalId.mint()
    with sqlite3.connect(waits.path) as db:
        before = db.execute("SELECT goal_id, wait FROM stored_goal_wait ORDER BY goal_id").fetchall()
        db.execute("INSERT INTO goal_wait_target VALUES (?, ?, ?)", (orphan, 0, target.lookup))
        db.execute("UPDATE goal_wait_target SET target_lookup=? WHERE goal_id=?",
                   (RecordedThreadIncarnation("other", 42.0).lookup, goal.id))
        index = GoalWaitTargetRow.indexes[0].name(GoalWaitTargetRow)
        db.execute(f'DROP INDEX "{index}"')
        db.commit()
        assert db.execute("PRAGMA foreign_key_check").fetchall()
        selected = ReconciledFile.existing_in(waits.root)[waits.path]
        assert selected is GoalWaits
        assert selected.reconcile_schema(db, WriterOwnedFile())
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT goal_id, wait FROM stored_goal_wait ORDER BY goal_id").fetchall() == before
    assert waits.targeting(target) == (original,)
    assert waits.clear(goal.id)
    with waits.snapshot() as db:
        assert GoalWaitTargetRow.count(db) == 0


def test_finished_turn_reads_only_its_waits_after_rename(tmp_path, monkeypatch):
    unrelated_goal_ids = tuple(GoalId.mint() for _ in range(200))
    comms, goal = _waiting(tmp_path)
    original = comms.goals.waits.read()[goal.id]
    comms.goals.waits.record_many(
        replace(original, goal_id=unrelated_goal_ids[index], targets=(GoalWaitTarget(
            RecordedThreadIncarnation(f"other-{index}", 10.0 + index)),))
        for index in range(200)
    )
    fence = _finish(comms, "child", "child-turn")
    comms.registry.rename("child", "renamed-child")
    decoded = []
    validate = RecordedGoalWait.__post_init__

    def decoded_wait(wait):
        decoded.append(wait.goal_id)
        validate(wait)

    def refuse_full_read(self):
        raise AssertionError("Terminal release read the entire wait store")

    with monkeypatch.context() as guard:
        guard.setattr(RecordedGoalWait, "__post_init__", decoded_wait)
        guard.setattr(GoalWaits, "read", refuse_full_read)
        assert comms.goals.release_waits_after_terminal_turn(fence) == ("owner",)
    # Capturing and validating this same wait may each decode it; unrelated
    # rows must never be decoded by either read.
    assert set(decoded) == {goal.id}
    assert len(comms.goals.waits.read()) == 200
