"""Independent P60 control for the heartbeat's mechanical-merge writer."""

from agent_comms.planning.goal_states import CompletedGoal
from test_problem_goal_completion import AssignedProblems, assigned_problems


def test_mechanical_merge_retains_finished_goal(assigned_problems: AssignedProblems) -> None:
    scene = assigned_problems
    current = scene.store.get("P1")
    merged = current.order.merged(scene.source.main_commit)
    closed = current.order.run.after_merge(current, merged)
    assert closed.assignment is None
    scene.store.record_merges((closed,))
    assert scene.store.get(current.id).assignment is None
    completed = scene.complete()
    assert isinstance(completed.state, CompletedGoal)
    assert tuple(todo.id for todo in scene.store.closed_for_goal(
        scene.owner.incarnation, scene.goal.id,
    )) == (current.id,)
