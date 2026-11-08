"""A problem Goal completes when its problems are done, and says so when it has none.

A problem Goal used to refuse completion unconditionally, so the only way out of one was to
replace it; and once every problem closed, `for_goal` returned no rows and the Goal looked
like one that had never been assigned anything:

    comms_goal status=completed                        -> "needs a typed claim"
    comms_goal status=completed claims=[merged ...]    -> "The problem Goal has no assigned Todos."

Both rules are exercised here against the store's two answers: the open problems, and every
problem the Goal ever assigned.
"""

from types import SimpleNamespace

import pytest

from agent_comms.goals import GoalRefused, ProblemGoalOrder


class Store:
    """The two answers the Goal rules ask for, and nothing else."""

    def __init__(self, open_problems, assigned):
        self.open_problems = open_problems
        self.assigned = assigned

    def for_goal(self, owner, goal_id):
        return self.open_problems

    def closed_for_goal(self, owner, goal_id):
        return self.assigned


def goals_with(*, open_problems, assigned):
    return SimpleNamespace(todos=Store(open_problems, assigned))


GOAL = SimpleNamespace(id="goal-1")
ORDER = ProblemGoalOrder()


def test_an_open_problem_keeps_the_goal_from_completing():
    goals = goals_with(open_problems=(object(),), assigned=(object(),))
    with pytest.raises(GoalRefused):
        ORDER.require_completion(GOAL, goals, owner=object())


def test_a_goal_whose_problems_are_all_closed_completes():
    goals = goals_with(open_problems=(), assigned=(object(),))
    ORDER.require_completion(GOAL, goals, owner=object())


def test_a_goal_that_was_never_assigned_anything_still_refuses():
    goals = goals_with(open_problems=(), assigned=())
    with pytest.raises(ValueError):
        ORDER.require_order(GOAL, goals, owner=object())


def test_a_finished_goal_is_still_addressable():
    goals = goals_with(open_problems=(), assigned=(object(),))
    ORDER.require_order(GOAL, goals, owner=object())
