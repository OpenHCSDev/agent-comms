"""Each unit edit is read under exactly one held order, and stays unjudged until a verdict
names it: a send-back reissuing the order judges no other edit."""
from dataclasses import replace
import time

import pytest

from test_judge_dropped import recorded_edit, unjudged
from test_todos import order

from agent_comms.field_codec import GoalId
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import ProblemGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.storage_rules import StorageRule
from agent_comms.order_tools import CommsVerdictTool
from agent_comms.todos import AcceptedVerdict, SendBackVerdict, StrikeReason, TodoError
from agent_comms.tool_caller import ThreadCaller
from agent_comms.owner_requests import NewestOpenRequest

pytest_plugins = ("test_held_problem_orders",)


@pytest.fixture
def two_orders(held_commander, cited_source):
    """The worker holds P1 and a newer P2; each problem has its own declared checkout."""
    ctx = held_commander
    lead = ctx.comms.registry.require(ctx.commander)
    worker = ctx.comms.registry.require("worker")
    ctx.comms.goals.todos.create(
        "P2", ctx.repos[0].github, "Repair the second workflow", creator=lead,
        assigner=lead.incarnation, owner=worker, generation=GoalId.mint(),
        request=NewestOpenRequest().for_order(ctx.comms.goals.todos, lead.incarnation),
        review_at=RecordedEventTime(time.time() + 1000),
        order=replace(order(cited_source), id="second-order", at=RecordedEventTime(20.0)),
    )
    rule = StorageRule.load(ctx.root)
    checkout = {problem: rule.problem_checkouts(rule.roots_of(worker), problem)[0]
                for problem in ("P1", "P2")}
    return ctx, checkout


def test_an_edit_belongs_to_the_order_whose_checkout_it_is_in(two_orders):
    ctx, checkout = two_orders
    recorded_edit(ctx, "p1-call", str(checkout["P1"] / "one.py"), "ONE = 'first problem'")
    recorded_edit(ctx, "p2-call", str(checkout["P2"] / "two.py"), "TWO = 'second problem'")
    recorded_edit(ctx, "scratch-call", str(ctx.root / "scratch" / "notes.md"), "notes for the PR")
    filed = [(edit.id, edit.problem) for edit in unjudged(ctx).values()]
    assert sorted(filed) == [("p1-call", "P1"), ("p2-call", "P2"), ("scratch-call", "P2")]


def test_a_send_back_leaves_the_edits_it_does_not_name_unjudged(two_orders):
    ctx, checkout = two_orders
    recorded_edit(ctx, "early-call", str(checkout["P1"] / "early.py"), "EARLY = 'not cited'")
    recorded_edit(ctx, "sent-call", str(checkout["P1"] / "sent.py"), "SENT = 'cited'")
    current = ctx.comms.goals.todos.get("P1")
    ctx.comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(), goal_id=current.held().generation))
    lead = ctx.comms.registry.require(ctx.commander).incarnation
    reissued = ThreadCaller(ctx.commander).invoke(CommsVerdictTool(problem="P1", operation=SendBackVerdict(
        current.revision, lead, current.held(), "sent-call",
        reason=StrikeReason("The worker restated the owner's decision in a second copied branch."),
        evidence=(current.order.checked().owner,))), ctx.comms).todo
    assert reissued.order.id != current.order.id
    assert set(unjudged(ctx)) == {"early-call"}


def test_a_second_verdict_on_one_edit_is_refused(two_orders):
    ctx, checkout = two_orders
    recorded_edit(ctx, "judged-call", str(checkout["P1"] / "judged.py"), "JUDGED = 'once'")
    store = ctx.comms.goals.todos
    lead = ctx.comms.registry.require(ctx.commander).incarnation
    current = store.get("P1")
    verdict = AcceptedVerdict(current.revision, lead, current.held(), "judged-call",
                              quote="Report the delivered commit")
    judged = store.apply("P1", verdict)
    with pytest.raises(TodoError, match="already has a verdict"):
        store.apply("P1", replace(verdict, expected_revision=judged.revision))
