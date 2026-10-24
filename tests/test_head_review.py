"""Head decisions through the native boundary, real stores, and the external gh process."""

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest


from agent_comms.commander import judge, order
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.head_review import PullRequestHead
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import OrderRefusingStandingRule
from agent_comms.errors import RelationViolationError, ToolRefusedError
from agent_comms.tool_caller import ThreadCaller
from agent_comms.work_branches import SourceGrowth
from agent_comms.order_tools import CommsHeadVerdictTool
from agent_comms.todos import (
    GrownHeadVerdict, GrowthReason, HeadReviewOrder, NoHeadOrder, PullRequestLink,
    RecordPullRequest, RejectedHeadVerdict, ReviewedHeadVerdict, StoredHeadReview,
    StrikeReason, UnmergedListedPullRequest, VerdictReason,
)
from source_revision_fixture import SourceRepository

pytest_plugins = ("test_holder_standing_rules", "github_fixture")

REASON = StrikeReason("This head repeats the same decision in two unrelated places instead of using the existing class.")
ACCEPTED = VerdictReason("The existing class owns this behavior without another copy")


@pytest.fixture
def reviewed_source(held_bus, configured_repository, tmp_path):
    """The registered checkout whose origin carries main and every reviewed pull request head."""
    source = SourceRepository.create(tmp_path / "reviewed", held_bus.goals.todos.get("P2").repo)
    configured_repository(held_bus, source)
    return source


def opened(github, source, number, head):
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    github.put(source.github, UnmergedListedPullRequest(
        number, f"fix/head-{number}", head, stamp, "main", reviewable_times=(stamp,),
    ))
    return PullRequestHead(source.github, number, head)


@pytest.fixture
def head_bus(held_bus, github_cli, reviewed_source):
    source = reviewed_source
    return held_bus, github_cli, opened(github_cli, source, 7, source.branch_commit)


def invoke(comms, request, verdict, order=NoHeadOrder()):
    tool = CommsHeadVerdictTool(request=request, operation=verdict, order=order)
    return tool.invoke(comms, tool.arguments, caller=ThreadCaller("lead"))


def test_orderless_head_family_persists_both_outcomes_and_posts_each_exact_head(head_bus):
    comms, github, request = head_bus
    before = tuple(todo.strikes for todo in comms.goals.todos.list(request.repo))
    accepted, rejected = ReviewedHeadVerdict(ACCEPTED), RejectedHeadVerdict(REASON)
    invoke(comms, replace(request, head=request.head[:9]), accepted)
    invoke(comms, request, rejected)
    with comms.goals.todos.snapshot() as db:
        records = tuple(row.record for row in StoredHeadReview.select(db, order_by=("seq",)))
    assert tuple(record.verdict for record in records) == (accepted, rejected)
    assert all(record.request == request and record.order == NoHeadOrder() for record in records)
    assert comms.goals.todos.head_reviews(request)[0].verdict == rejected
    assert tuple(todo.strikes for todo in comms.goals.todos.list(request.repo)) == before
    posts = tuple(call for call in github.calls() if f"statuses/{request.head}" in call[1])
    assert len(posts) == 2
    assert "state=success" in posts[0] and "state=failure" in posts[1]


def test_judge_ok_and_back_dispatch_orderless_prs_without_positional_edit_ids(head_bus):
    comms, github, request = head_bus
    context = order.Commander.read_root(comms.root)
    why = ACCEPTED.text
    common = ["--repo", request.repo.to_wire(), "--pr", str(request.pr), "--head", request.head,
              "--why", why]
    judge.main(["ok", *common], context)
    assert isinstance(comms.goals.todos.head_reviews(request)[0].verdict, ReviewedHeadVerdict)
    todo = comms.goals.todos.get("P2")
    location = f"{todo.order.check.directory / todo.order.owner.file}:{todo.order.owner.line}"
    judge.main(["back", *common,
                "--roast", "This is stupid because the same decision is written twice instead of belonging to its existing class.",
                "--fix", "Put the shared behavior on the existing parent and delete both copies.",
                "--slop", "IMPL-5", "--fact", "the same decision is written twice",
                "--at", location, "--derive", "base-method", "--by", location], context)
    assert isinstance(comms.goals.todos.head_reviews(request)[0].verdict, RejectedHeadVerdict)
    assert len(tuple(call for call in github.calls() if f"statuses/{request.head}" in call[1])) == 2


def test_moved_head_posts_and_records_nothing(head_bus):
    comms, github, request = head_bus
    moved = replace(request, head="a" * 40)
    with pytest.raises(ToolRefusedError):
        invoke(comms, moved, ReviewedHeadVerdict(ACCEPTED))
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
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED), scope)
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


def test_acceptance_over_the_growth_limit_needs_a_reason_naming_each_growing_file(
        head_bus, reviewed_source):
    comms, github, _ = head_bus
    source, grown = reviewed_source, SourceGrowth.limit + 1
    module = "".join(f"value_{line} = {line}\n" for line in range(grown))
    blob = source.git_input(source.checkout, module, "hash-object", "-w", "--stdin")
    tree = source.git_input(source.checkout, f"100644 blob {blob}\tgrown.py\n", "mktree")
    tree = source.git_input(source.checkout, f"040000 tree {tree}\tsrc\n", "mktree")
    head = source.git(source.checkout, "commit-tree", tree,
                      "-p", source.main_commit, "-m", "Grow").strip()
    source.git(source.checkout, "push", "-q", "origin", f"{head}:refs/pull/8/head")
    request = opened(github, source, 8, head)
    with pytest.raises(ToolRefusedError, match=f"src/grown.py \\+{grown}"):
        invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    with pytest.raises(ToolRefusedError, match="names no reason for src/grown.py"):
        invoke(comms, request, GrownHeadVerdict(ACCEPTED, GrowthReason(
            "overgrown.py declares forty one values that the reviewed request needs as names")))
    assert not comms.goals.todos.head_reviews(request)
    invoke(comms, request, GrownHeadVerdict(ACCEPTED, GrowthReason(
        "grown.py declares forty one values that the reviewed request needs as separate names")))
    posts = tuple(call for call in github.calls() if f"statuses/{head}" in call[1])
    assert len(posts) == 1 and any(f"(src +{grown})" in part for part in posts[0])
