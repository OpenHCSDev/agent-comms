"""Head decisions through the native boundary, real stores, and the external gh process."""

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from pathlib import Path

from agent_comms.commander import judge, order
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.head_review import PullRequestHead
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import OrderRefusingStandingRule
from agent_comms.errors import RelationViolationError, ToolRefusedError
from agent_comms.tool_caller import ThreadCaller
from agent_comms.order_tools import CommsHeadVerdictTool
from agent_comms.todos import (
    HeadReviewOrder, NoHeadOrder, PullRequestLink, RecordPullRequest,
    RejectedHeadVerdict, ReviewedHeadVerdict, StoredHeadReview, StrikeReason,
    UnmergedListedPullRequest,
)

pytest_plugins = ("test_holder_standing_rules", "github_fixture")

HEAD = "c" * 40
REASON = StrikeReason("This head repeats the same decision in two unrelated places instead of using the existing class.")


@pytest.fixture
def head_bus(held_bus, github_cli):
    todo = held_bus.goals.todos.get("P2")
    github_cli.configure(todo.repo)
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    github_cli.put(todo.repo, UnmergedListedPullRequest(
        7, "fix/head", HEAD, stamp, "main", reviewable_times=(stamp,),
    ))
    return held_bus, github_cli, PullRequestHead(todo.repo, 7, HEAD)


def invoke(comms, request, verdict, order=NoHeadOrder()):
    tool = CommsHeadVerdictTool(request=request, operation=verdict, order=order)
    return tool.invoke(comms, tool.arguments, caller=ThreadCaller("lead"))


def test_orderless_head_family_persists_both_outcomes_and_posts_each_exact_head(head_bus):
    comms, github, request = head_bus
    before = tuple(todo.strikes for todo in comms.goals.todos.list(request.repo))
    accepted, rejected = ReviewedHeadVerdict(), RejectedHeadVerdict(REASON)
    invoke(comms, replace(request, head=HEAD[:9]), accepted)
    invoke(comms, request, rejected)
    with comms.goals.todos.snapshot() as db:
        records = tuple(row.record for row in StoredHeadReview.select(db, order_by=("seq",)))
    assert tuple(record.verdict for record in records) == (accepted, rejected)
    assert all(record.request == request and record.order == NoHeadOrder() for record in records)
    assert comms.goals.todos.head_reviews(request)[0].verdict == rejected
    assert tuple(todo.strikes for todo in comms.goals.todos.list(request.repo)) == before
    posts = tuple(call for call in github.calls() if f"statuses/{HEAD}" in call[1])
    assert len(posts) == 2
    assert "state=success" in posts[0] and "state=failure" in posts[1]


def test_judge_ok_and_back_dispatch_orderless_prs_without_positional_edit_ids(head_bus):
    comms, github, request = head_bus
    context = order.Commander.read_root(comms.root)
    why = "The existing class owns this behavior without another copy"
    common = ["--pr", str(request.pr), "--head", request.head, "--why", why]
    judge.main(["ok", *common], context)
    assert isinstance(comms.goals.todos.head_reviews(request)[0].verdict, ReviewedHeadVerdict)
    todo = comms.goals.todos.get("P2")
    location = f"{Path(todo.order.check.directory) / todo.order.owner.file}:{todo.order.owner.line}"
    judge.main(["back", *common,
                "--roast", "This is stupid because the same decision is written twice instead of belonging to its existing class.",
                "--fix", "Put the shared behavior on the existing parent and delete both copies.",
                "--slop", "IMPL-5", "--fact", "the same decision is written twice",
                "--at", location, "--derive", "base-method", "--by", location], context)
    assert isinstance(comms.goals.todos.head_reviews(request)[0].verdict, RejectedHeadVerdict)
    assert len(tuple(call for call in github.calls() if f"statuses/{HEAD}" in call[1])) == 2


def test_moved_head_posts_and_records_nothing(head_bus):
    comms, github, request = head_bus
    moved = replace(request, head="a" * 40)
    with pytest.raises(ToolRefusedError):
        invoke(comms, moved, ReviewedHeadVerdict())
    assert not comms.goals.todos.head_reviews(moved)
    assert not any("statuses/" in call[1] for call in github.calls())


def test_ready_orderless_review_refuses_backend_orders_until_a_recorded_decision(head_bus):
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    assert OrderRefusingStandingRule.refusals(comms.goals, "lead")
    action = CreateProblemGoalAction(
        problem="P3", commander="lead", request=todo.request,
        assigner=comms.registry.require("lead").incarnation,
        repo=todo.repo, text="Repair the next owner", order=todo.order.checked(),
        review_at=RecordedEventTime(time.time() + 3600),
    )
    with pytest.raises(RelationViolationError):
        comms.goals.update_goal("outsider", action)
    assert all(current.id != "P3" for current in comms.goals.todos.list(request.repo))
    invoke(comms, request, RejectedHeadVerdict(REASON))
    assert not OrderRefusingStandingRule.refusals(comms.goals, "lead")
    comms.goals.update_goal("outsider", action)
    assert comms.goals.todos.get("P3").holder == "outsider"


def test_linked_head_rejection_strikes_revises_and_notifies_its_holder(head_bus):
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(comms.goals, todo.id)
    todo = comms.goals.todos.get(todo.id)
    scope = HeadReviewOrder.for_request(
        (todo,), request, "Repair the existing owner", (todo.order.checked().owner,),
    )
    invoke(comms, request, ReviewedHeadVerdict(), scope)
    todo = comms.goals.todos.get(todo.id)
    scope = HeadReviewOrder.for_request(
        (todo,), request, "Repair the existing owner", (todo.order.checked().owner,),
    )
    before = comms.registry.require("worker").goal.require()
    invoke(comms, request, RejectedHeadVerdict(REASON), scope)
    changed = comms.goals.todos.get(todo.id)
    assert changed.strike_count == todo.strike_count + 1
    assert changed.order.checked().id != todo.order.checked().id
    assert REASON.text in changed.order.checked().edits
    assert comms.registry.require("worker").goal.require().revision > before.revision
    assert comms.goals.todos.head_reviews(request)[0].order.problem == todo.id
