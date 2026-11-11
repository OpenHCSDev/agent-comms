"""Correct mistaken completion through the public problem operation, retaining its work."""
import os
from dataclasses import replace

import pytest
from agent_comms_hooks import ToolRefusedError

from agent_comms.errors import TodoError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.installer.runtime_resolution import ToadPackage
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning import orders
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.planning.goals import CountCheckResult, OrderCheck
from agent_comms.planning.orders import (
    ChangeTodoState, DoneTodoState, OpenTodoState, PendingProblemOrder,
    RestoreHolderTodo, TriageScope, WithdrawTodo,
)
from agent_comms.planning.pull_requests import PullRequestLink
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.registry.threads import ExternalThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.transport.tools.orders import CommsChangeProblemTool, CommsOrderDoneTool
from test_todos import order, order_pr

pytest_plugins = ("test_todos",)


@pytest.fixture(params=(False, True), ids=("source", "triage-report"))
def completed_draft(pr_team, request):
    ctx = pr_team
    comms, source = ctx.comms, ctx.repos[0]
    owner = comms.registry.declare(ExternalThread(
        "planner", frozenset(), Checkout(source.checkout),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    goal = comms.goals.update_goal(owner.name, SetGoalAction(text="Finish the remaining original work"))
    declared = replace(order(source), check=OrderCheck(source.checkout, "printf 1", CountCheckResult(1)))
    if request.param:
        declared = replace(declared, scope=TriageScope())
    ctx = type(ctx).on_root(owner.name, ctx.repos, ctx.root)
    creator = owner
    owner_request = comms.goals.todos.add_request(creator.incarnation, "Finish every required phase").id
    original = comms.goals.todos.create(
        "P1", ToadPackage.repository if request.param else source.github,
        "Finish the original plan, not just its first report",
        creator=creator, assigner=creator.incarnation, owner=owner,
        generation=goal.id, request=owner_request, review_at=RecordedEventTime(1000),
        order=declared.merged(Repo.git(source.checkout, "rev-parse", "origin/main").strip()),
    )
    ctx.call(CommsChangeProblemTool(problem=original.id, operation=ChangeTodoState(
        original.revision, DoneTodoState, creator.incarnation, None,
    )))
    closed = comms.goals.todos.get(original.id)
    assert closed.state is DoneTodoState and closed.assignment is None
    assert comms.registry.require(owner.name).goal == goal
    return ctx, original, closed, goal


def reopen(ctx, original, closed):
    return orders.ReopenTodo(
        expected_revision=closed.revision,
        actor=ctx.comms.registry.require(ctx.actor_name).incarnation,
        previous=original.held(),
        check=OrderCheck(ctx.repos[0].checkout, "printf 0", CountCheckResult(1)),
        review=orders.ReviewedOrderRun(
            "A peer reviewed the exact head against original owners and all remaining requirements."),
        reason="The first report was merged while required design work remains unfinished.",
    )


def test_closed_restore_requires_a_public_reopening(completed_draft):
    ctx, original, closed, goal = completed_draft
    with pytest.raises(TodoError, match="done"):
        RestoreHolderTodo(closed.revision,
            ctx.comms.registry.require(ctx.actor_name).incarnation, original.held()).apply(closed)
    assert ctx.comms.goals.todos.get(closed.id) == closed


@pytest.mark.parametrize("prerequisite_passes", (False, True))
def test_public_reopen_preserves_work_goal_report_and_history(completed_draft, prerequisite_passes):
    ctx, original, closed, goal = completed_draft
    command = reopen(ctx, original, closed)
    if prerequisite_passes:
        command = replace(command, check=original.order.check)
    wire = FieldCodec.encode(CommsChangeProblemTool(problem=closed.id, operation=command))
    ctx.call(FieldCodec.decode(CommsChangeProblemTool, wire))
    restored = ctx.comms.goals.todos.get(closed.id)
    assert restored.state is OpenTodoState
    assert restored.assignment == original.assignment
    assert isinstance(restored.order, PendingProblemOrder)
    assert restored.order.id == original.order.id
    assert restored.order.scope == original.order.scope
    assert restored.order.edits == original.order.edits
    assert restored.order.run == command.review
    with pytest.raises(ToolRefusedError, match="merge never closes"):
        restored.order.merged(original.order.sha).require_close()
    assert restored.order.corrections[-1].previous == original.order.check
    assert (restored.request, restored.review_at) == (original.request, original.review_at)
    assert ctx.comms.registry.require(original.holder).goal == goal
    assert closed.order in restored.order_history
    assert restored.strike_count == original.strike_count
    assert not restored.order.closed_by_merge(restored, orders.ProgressRead(),
                                              ctx.comms.registry.storage_rule.checkout_base)
    if isinstance(restored.order.scope, TriageScope):
        assert not restored.order.pull_requests
        with pytest.raises(ValueError):
            ctx.call(CommsOrderDoneTool(problem=restored.id, order_id=restored.order.id,
                delivery=orders.ReportDelivery(report="The report file exists."), claims=()))
        assert ctx.comms.goals.todos.get(restored.id) == restored
        ctx.call(CommsOrderDoneTool(problem=restored.id, order_id=restored.order.id,
            delivery=orders.ReportDelivery(report=command.review.text), claims=()))
        assert ctx.comms.goals.todos.get(restored.id).state is DoneTodoState
    else:
        link = PullRequestLink(ctx.repos[0].github, 2)
        order_pr(ctx, ctx.repos[0].checkout, restored.id, "--existing", str(link.number))
        assert ctx.comms.goals.todos.get(restored.id).order.pull_requests == (link,)


def test_reopen_refuses_stale_revision_holder_goal_and_old_success_check(completed_draft):
    ctx, original, closed, goal = completed_draft
    command = reopen(ctx, original, closed)
    bad = (
        replace(command, expected_revision=closed.revision - 1),
        replace(command, previous=replace(original.held(), owner="someone-else")),
    )
    for correction in bad:
        with pytest.raises((TodoError, ValueError)):
            ctx.call(CommsChangeProblemTool(problem=closed.id, operation=correction))
        assert ctx.comms.goals.todos.get(closed.id) == closed
    ctx.comms.goals.update_goal(original.holder, SetGoalAction(text="A different goal now owns the thread"))
    with pytest.raises((TodoError, ValueError)):
        ctx.call(CommsChangeProblemTool(problem=closed.id, operation=command))
    assert ctx.comms.goals.todos.get(closed.id) == closed


def test_prepared_shared_maintenance_restores_only_the_recorded_holder(completed_draft):
    ctx, original, closed, goal = completed_draft
    command = reopen(ctx, original, closed)
    with pytest.raises(TodoError, match="Prepare the corrected order check"):
        command.apply(closed)
    peer = type(ctx).on_root("colleague", ctx.repos, ctx.root)
    peer.call(CommsChangeProblemTool(problem=closed.id, operation=command))
    restored = ctx.comms.goals.todos.get(closed.id)
    assert restored.assignment == original.assignment
    assert ctx.comms.registry.require(original.holder).goal == goal


def test_reopen_does_not_reverse_an_intentional_withdrawal(completed_draft):
    ctx, original, closed, goal = completed_draft
    command = reopen(ctx, original, closed)
    ctx.call(CommsChangeProblemTool(problem=closed.id, operation=command))
    restored = ctx.comms.goals.todos.get(closed.id)
    ctx.call(CommsChangeProblemTool(problem=restored.id, operation=WithdrawTodo(
        expected_revision=restored.revision, actor=command.actor,
        why="The owner intentionally withdrew this work after reconsidering its requirements.",
    )))
    withdrawn = ctx.comms.goals.todos.get(closed.id)
    with pytest.raises(TodoError, match="merged completion"):
        ctx.call(CommsChangeProblemTool(problem=closed.id,
            operation=replace(command, expected_revision=withdrawn.revision)))
    assert ctx.comms.goals.todos.get(closed.id) == withdrawn
