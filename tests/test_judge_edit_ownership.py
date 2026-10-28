"""Each unit edit is read under exactly one held order, and stays unjudged until a verdict
names it: a send-back reissuing the order judges no other edit."""
import time
from dataclasses import replace

import pytest

from agent_comms.errors import TodoError
from agent_comms.field_codec import (
    GoalId,
    OrderId,
    ToolCallId,
)
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import ProblemGoalOrder
from agent_comms.order_tools import CommsVerdictTool
from agent_comms.owner_requests import NewestOpenRequest
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.storage_rules import StorageRule
from agent_comms.todos import (
    AcceptedVerdict, Assignment, SendBackVerdict, StrikeReason, Todo,
)
from agent_comms.tool_caller import ThreadCaller
from test_judge_dropped import recorded_edit, unjudged
from test_todos import order

pytest_plugins = ("test_held_problem_orders",)


@pytest.fixture
def two_orders(held_commander, cited_source):
    """The worker holds P1 and a newer P2; each problem has its own declared checkout."""
    ctx = held_commander
    lead = ctx.comms.registry.require(ctx.commander)
    worker = ctx.comms.registry.require("worker")
    # Core refuses a unit a second, unrelated order; a bus saved before that rule can hold
    # one, so the newer P2 is written as that saved row.
    request = NewestOpenRequest().for_order(ctx.comms.goals.todos, lead.incarnation)
    with ctx.comms.goals.todos.writing() as db:
        Todo.ordered(
            "P2", ctx.repos[0].github, "Repair the second workflow", lead.incarnation,
            Assignment.of(worker.incarnation, lead.incarnation, GoalId.mint()),
            RecordedEventTime(time.time() + 1000),
            replace(order(cited_source), id=OrderId.mint(), at=RecordedEventTime(20.0)),
            request,
        ).insert_if_absent(db)
    rule = StorageRule.load(ctx.root)
    checkout = {problem: rule.order_checkout(rule.roots_of(worker), problem)
                for problem in ("P1", "P2")}
    for problem, path in checkout.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        cited_source.git(cited_source.checkout, "worktree", "add", "-q", "-b",
                         f"edit-ownership-{problem}", str(path))
    return ctx, checkout


def test_an_edit_belongs_to_the_order_whose_checkout_it_is_in(two_orders):
    ctx, checkout = two_orders
    recorded_edit(ctx, ToolCallId("p1-call"), str(checkout["P1"] / "one.py"), "ONE = 'first problem'")
    recorded_edit(ctx, ToolCallId("p2-call"), str(checkout["P2"] / "two.py"), "TWO = 'second problem'")
    recorded_edit(ctx, ToolCallId("scratch-call"), str(ctx.root / "scratch" / "notes.md"), "notes for the PR")
    filed = [(edit.id, edit.problem) for edit in unjudged(ctx).values()]
    assert sorted(filed) == [("p1-call", "P1"), ("p2-call", "P2")]
    assert ctx.comms.goals.todos.get("P2").judged_calls == ()


def test_a_send_back_leaves_the_edits_it_does_not_name_unjudged(two_orders):
    ctx, checkout = two_orders
    recorded_edit(ctx, ToolCallId("early-call"), str(checkout["P1"] / "early.py"), "EARLY = 'not cited'")
    recorded_edit(ctx, ToolCallId("sent-call"), str(checkout["P1"] / "sent.py"), "SENT = 'cited'")
    current = ctx.comms.goals.todos.get("P1")
    ctx.comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(), goal_id=current.held().generation))
    lead = ctx.comms.registry.require(ctx.commander).incarnation
    reissued = ThreadCaller(ctx.commander).invoke(CommsVerdictTool(problem="P1", operation=SendBackVerdict(
        current.revision, lead, current.held(), ToolCallId("sent-call"),
        reason=StrikeReason("The worker restated the owner's decision in a second copied branch."),
        evidence=(current.order.checked().owner,))), ctx.comms).todo
    assert reissued.order.id != current.order.id
    assert set(unjudged(ctx)) == {"early-call"}


def test_a_second_verdict_on_one_edit_is_refused(two_orders):
    ctx, checkout = two_orders
    recorded_edit(ctx, ToolCallId("judged-call"), str(checkout["P1"] / "judged.py"), "JUDGED = 'once'")
    store = ctx.comms.goals.todos
    lead = ctx.comms.registry.require(ctx.commander).incarnation
    current = store.get("P1")
    verdict = AcceptedVerdict(current.revision, lead, current.held(), ToolCallId("judged-call"),
                              quote="Report the delivered commit")
    judged = store.apply("P1", verdict)
    with pytest.raises(TodoError, match="already has a verdict"):
        store.apply("P1", replace(verdict, expected_revision=judged.revision))


def test_an_edit_in_an_unknown_problems_checkout_joins_no_held_order(two_orders, cited_source):
    ctx, checkout = two_orders
    released = checkout["P1"].with_name(checkout["P1"].name.replace("--P1", "--P9"))
    cited_source.git(cited_source.checkout, "worktree", "add", "-q", "-b", "unknown-P9", str(released))
    recorded_edit(ctx, ToolCallId("p9-call"), str(released / "nine.py"), "NINE = 'unknown problem'")
    recorded_edit(ctx, ToolCallId("p2-call"), str(checkout["P2"] / "two.py"), "TWO = 'second problem'")
    assert [(edit.id, edit.problem) for edit in unjudged(ctx).values()] == [("p2-call", "P2")]


def test_reading_a_stored_reason_never_flags_its_words(capsys):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.todos import VerdictReason

    FieldCodec.decode(VerdictReason, "the store cutover happened before this verdict was written")
    assert capsys.readouterr().err == ""
    VerdictReason.refused("the store cutover happened before this verdict was written")
    assert "Flag: jargon cutover" in capsys.readouterr().err
