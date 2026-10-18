"""Goal requirements own their matching rule and refusal, not the goal record."""

from dataclasses import dataclass

from .errors import RelationViolationError


class GoalSelection:
    error = RelationViolationError
    refusal = "No goal is set for this thread."

    def absent(self, goal, **context):
        raise self.error(self.refusal)

    def matches(self, goal):
        return True

    def matches_absence(self):
        return False

    def select(self, goal, **context):
        if not self.matches(goal):
            self.absent(goal)
        return goal


@dataclass(frozen=True)
class GoalIdentity(GoalSelection):
    id: str
    refusal = "This goal was replaced or cleared; refresh its state."

    def matches(self, goal):
        return self.id == goal.id


@dataclass(frozen=True)
class AbsentGoalCheckpoint(GoalSelection):
    def matches(self, goal):
        return False

    def matches_absence(self):
        return True

    def absent(self, goal, **context):
        return goal
