from dataclasses import replace

from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import GoalWaits, RecordedGoalWait
from agent_comms.thread_identity import RecordedThreadIncarnation
from test_goal_standby_liveness import _finish, _waiting


def test_finished_turn_reads_only_its_waits_after_rename(tmp_path, monkeypatch):
    comms, goal = _waiting(tmp_path)
    original = comms.goals.waits.read()[goal.id]
    comms.goals.waits.record_many(
        replace(original, goal_id=f"unrelated-{index}", targets=(GoalWaitTarget(
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
    assert decoded == [goal.id]
    assert len(comms.goals.waits.read()) == 200
