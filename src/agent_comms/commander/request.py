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
from .context import CommanderContext
from .order import Commander, CommanderVerb, RuledVerb, hhmm


def open_requests(commander: CommanderContext) -> tuple[tuple[Goal, tuple[Todo, ...]], ...]:
    """The commander's open owner requests and their undelivered orders."""
    thread = commander.comms.registry.require(commander.commander)
    return tuple(
        request
        for goal in thread.goal.active()
        for request in goal.order.open_requests(goal, commander.comms.goals, thread.incarnation)
    )


class RequestVerb(CommanderVerb):
    """A verb of `request`."""


@dataclass(frozen=True, kw_only=True)
class RequestAddVerb(RequestVerb, RuledVerb, declared_name="add"):
    help = "record an owner request as the commander's Goal; prints its id"
    text: str = option("text")

    def run(self, ctx: Commander) -> None:
        goal = ctx.comms.goals.update_goal(
            ctx.commander, SetGoalAction(text=self.text, order=RequestGoalOrder())
        )
        print(goal.require().id)


@dataclass(frozen=True, kw_only=True)
class RequestCompleteVerb(RequestVerb, RuledVerb, declared_name="complete"):
    help = "close a request; refused while an order serving it is open"
    id: str = option("id")
    why: str = option("--why")

    def run(self, ctx: Commander) -> None:
        expect = GoalPrecondition(goal_id=self.id, expected_state=ActiveGoal())
        ctx.comms.goals.update_goal(
            ctx.commander, CompletedGoalAction(progress=self.why, expect=expect)
        )
        print(f"{self.id} done")


class RequestListVerb(RequestVerb, declared_name="list"):
    help = "the open request and the open orders serving it"

    def run(self, ctx: Commander) -> None:
        requests = open_requests(ctx)
        if not requests:
            print("No open request.")
        for goal, served in requests:
            print(f"{goal.id} {goal.text}")
            for todo in served:
                print(f"  {todo.id} due {hhmm(todo.deadline)}: {todo.text}")


def main(a: list[str], commander: Commander) -> None:
    RequestVerb.parse(a, __doc__).apply(commander)
