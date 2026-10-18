"""What the owner asked for, recorded as the commander's own Goal so no request is forgotten.

`request add "TEXT"` records one and prints its id; `request complete ID --why "..."` closes
it, refused while an order serving it is open; `request list` shows the open request with its
open orders. A thread holds one Goal, so one request is open at a time. Every order names the
request it serves (`order send|problem --request ID`); rotation prints REQUEST STALLED for an
open request that no open order serves.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..cli_commands import option
from ..goal_actions import CompletedGoalAction, GoalPrecondition, SetGoalAction
from ..goal_states import ActiveGoal
from ..goals import Goal, RequestGoalOrder
from ..todos import Todo
from .order import COMMS, ME, CommanderVerb, RuledVerb, hhmm


def open_request() -> tuple[Goal, tuple[Todo, ...]] | None:
    """The commander's active Goal when it is an owner request, with the orders serving it."""
    thread = COMMS.registry.require(ME)
    try:
        goal = thread.goal.require_active()
    except ValueError:
        return None
    served = goal.order.serving(goal, COMMS.goals, thread.incarnation)
    return None if served is None else (goal, served)


def stalled() -> list[str]:
    match open_request():
        case (goal, ()):
            return [
                f"REQUEST STALLED: {goal.id} {goal.text} -> order its next step or mark it done"
            ]
    return []


class RequestVerb(CommanderVerb):
    """A verb of `request`."""


@dataclass(frozen=True, kw_only=True)
class RequestAddVerb(RequestVerb, RuledVerb, declared_name="add"):
    help = "record an owner request as the commander's Goal; prints its id"
    text: str = option("text")

    def run(self) -> None:
        goal = COMMS.goals.update_goal(ME, SetGoalAction(text=self.text, order=RequestGoalOrder()))
        print(goal.require().id)


@dataclass(frozen=True, kw_only=True)
class RequestCompleteVerb(RequestVerb, RuledVerb, declared_name="complete"):
    help = "close a request; refused while an order serving it is open"
    id: str = option("id")
    why: str = option("--why")

    def run(self) -> None:
        expect = GoalPrecondition(goal_id=self.id, expected_state=ActiveGoal())
        COMMS.goals.update_goal(ME, CompletedGoalAction(progress=self.why, expect=expect))
        print(f"{self.id} done")


class RequestListVerb(RequestVerb, declared_name="list"):
    help = "the open request and the open orders serving it"

    def run(self) -> None:
        request = open_request()
        if request is None:
            print("No open request.")
            return
        goal, served = request
        print(f"{goal.id} {goal.text}")
        for todo in served:
            print(f"  {todo.id} due {hhmm(todo.deadline)}: {todo.text}")


def main(a: list[str]) -> None:
    RequestVerb.parse(a, __doc__).apply()
