"""Head decisions through the native boundary, real stores, and the external gh process."""

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest


from agent_comms.commander import judge, order
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.heartbeat_subjects import HeadSubject
from agent_comms.head_review import PullRequestHead
from agent_comms.heartbeat_subjects import HeadSubject, UnitSubject
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import (
    IdlePullRequest, OrderlessUnreviewedHead, OrderRefusingStandingRule, StuckMerge,
)
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import RecordProblems
from agent_comms.tool_caller import ThreadCaller
from agent_comms.work_branches import SourceGrowth
from agent_comms.order_tools import CommsHeadVerdictTool
from agent_comms.todos import (
    ConflictMergeBlock, DraftListedPullRequest, FailedCheckMergeBlock, GrownHeadVerdict, GrowthReason, OrderHeadReview, LinkedHeadReview,
    NoHeadOrder, ProgressRead, PullRequestLink, RecordPullRequest, RejectedHeadVerdict,
    ReviewedHeadVerdict, StoredHeadReview, UnquotedHeadReview,
    StrikeReason, UnderReviewHeadVerdict, UnmergedListedPullRequest, VerdictReason,
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
    before = tuple(todo.strike_count for todo in comms.goals.todos.list(request.repo))
    accepted, rejected = ReviewedHeadVerdict(ACCEPTED), RejectedHeadVerdict(REASON)
    invoke(comms, replace(request, head=request.head[:9]), accepted)
    invoke(comms, request, rejected)
    with comms.goals.todos.snapshot() as db:
        records = tuple(row.record for row in StoredHeadReview.select(db, order_by=("seq",)))
    assert tuple(record.verdict for record in records) == (accepted, rejected)
    assert all(record.request == request and record.order == NoHeadOrder() for record in records)
    assert comms.goals.todos.head_reviews(request)[0].verdict == rejected
    assert tuple(todo.strike_count for todo in comms.goals.todos.list(request.repo)) == before
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


def test_a_verdict_lists_every_argument_refusal_in_one_run_and_records_nothing(head_bus):
    """ok with empty --serves and both edit IDs and --pr took one run per refusal."""
    comms, github, request = head_bus
    context = order.Commander.read_root(comms.root)
    with pytest.raises(RecordProblems) as refused:
        judge.main(["ok", "E1:abcd", "--pr", str(request.pr), "--head", request.head,
                    "--why", ACCEPTED.text, "--serves", ""], context)
    assert {"subject", "--serves"} <= refused.value.problems.keys()
    assert "edit IDs or --pr" in refused.value.problems["subject"]
    assert "0 words" in refused.value.problems["--serves"]
    assert not comms.goals.todos.head_reviews(request)
    assert not any(f"statuses/{request.head}" in call[1] for call in github.calls())


def test_judge_reviewing_after_ok_withdraws_the_accepted_status_on_that_head(
        head_bus, reviewed_source, configured_repository, tmp_path):
    """GitHub keeps the newest status per context on a commit, so reviewing an approved head
    holds it out of the merge queue until a new ok."""
    comms, github, request = head_bus
    context = order.Commander.read_root(comms.root)
    second_checkout = tmp_path / "second-review-checkout"
    reviewed_source.git(reviewed_source.checkout, "worktree", "add", "-q", "--detach",
                        str(second_checkout))
    configured_repository(comms, reviewed_source,
                          reviewed_source.model_copy(update={"checkout": second_checkout}))
    project = comms.registry.snapshot().commander_project.require()
    assert project.repository_names().count(request.repo) == 1
    assert project.repository_names(str(second_checkout)) == (request.repo,)
    common = ["--pr", str(request.pr), "--head", request.head]
    judge.main(["ok", *common, "--why", ACCEPTED.text], context)
    judge.main(["reviewing", *common], context)
    posts = tuple(call for call in github.calls() if f"statuses/{request.head}" in call[1])
    assert [next(part for part in post if part.startswith("state=")) for post in posts] == [
        "state=success", "state=pending"]


def test_moved_head_posts_and_records_nothing(head_bus):
    comms, github, request = head_bus
    moved = replace(request, head="a" * 40)
    with pytest.raises(ToolRefusedError):
        invoke(comms, moved, ReviewedHeadVerdict(ACCEPTED))
    assert not comms.goals.todos.head_reviews(moved)
    assert not any("statuses/" in call[1] for call in github.calls())


def test_only_a_head_linked_to_the_callers_held_order_refuses_its_orders(head_bus):
    """2026-10-25: dqdock's orders froze on claude-code's orderless #3311."""
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    action = CreateProblemGoalAction(
        problem="P3", commander="lead", request=todo.request,
        assigner=comms.registry.require("lead").incarnation,
        repo=todo.repo, text="Repair the next owner", order=todo.order.checked(),
        review_at=RecordedEventTime(time.time() + 3600),
    )
    assert not OrderRefusingStandingRule.refusals(comms.goals, "lead")
    assert [rule.request for rule in OrderlessUnreviewedHead.scan_reviews(
        comms.goals, "lead", ProgressRead(()))] == [request]
    comms.goals.update_goal("outsider", action)
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(comms.goals, todo.id)
    assert OrderRefusingStandingRule.refusals(comms.goals, "lead")


def test_a_reviewing_mark_owes_the_judge_line_until_a_decision(head_bus):
    """A reviewing mark on an open head is not a verdict: the JUDGE line stays owed and its
    heartbeat line undisposed until ok or back, and reviewing after an ok owes it again."""
    comms, _, request = head_bus
    subject, since = HeadSubject(request), time.time() - 1

    def owed() -> list[PullRequestHead]:
        return [rule.request for rule in OrderlessUnreviewedHead.scan_reviews(
            comms.goals, "lead", ProgressRead(()))]

    invoke(comms, request, UnderReviewHeadVerdict())
    assert owed() == [request]
    assert not subject.disposed(comms, frozenset(), since)
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    assert owed() == []
    assert subject.disposed(comms, frozenset(), since)
    invoke(comms, request, UnderReviewHeadVerdict())
    assert owed() == [request]


def test_linked_head_rejection_strikes_revises_and_notifies_its_holder(head_bus):
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(comms.goals, todo.id)
    todo = comms.goals.todos.get(todo.id)
    scope = OrderHeadReview.for_request(
        (todo,), request, lambda linked: LinkedHeadReview.of(
            linked, "Repair the existing owner", (linked.order.checked().owner,)),
    )
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED), scope)
    todo = comms.goals.todos.get(todo.id)
    scope = OrderHeadReview.for_request(
        (todo,), request, lambda linked: UnquotedHeadReview.of(
            linked, (linked.order.checked().owner,)),
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


def test_a_stacked_head_is_charged_only_its_own_growth_over_its_base_branch(
        head_bus, reviewed_source):
    """2026-10-25: judge ok charged a PR stacked on another open PR for its parent's lines."""
    comms, github, _ = head_bus
    source = reviewed_source

    def commit(parent, lines, message):
        module = "".join(f"value_{line} = {line}\n" for line in range(lines))
        blob = source.git_input(source.checkout, module, "hash-object", "-w", "--stdin")
        tree = source.git_input(source.checkout, f"100644 blob {blob}\tgrown.py\n", "mktree")
        tree = source.git_input(source.checkout, f"040000 tree {tree}\tsrc\n", "mktree")
        return source.git(source.checkout, "commit-tree", tree, "-p", parent, "-m", message).strip()

    parent = commit(source.main_commit, SourceGrowth.limit + 1, "Grow the parent request")
    head = commit(parent, SourceGrowth.limit + 2, "Grow one line on top")
    source.git(source.checkout, "push", "-q", "origin",
               f"{parent}:refs/heads/fix/parent", f"{head}:refs/pull/9/head")
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    github.put(source.github, UnmergedListedPullRequest(
        9, "fix/child", head, stamp, "fix/parent", reviewable_times=(stamp,)))
    invoke(comms, PullRequestHead(source.github, 9, head), ReviewedHeadVerdict(ACCEPTED))
    posts = tuple(call for call in github.calls() if f"statuses/{head}" in call[1])
    assert len(posts) == 1 and any("(src +1)" in part for part in posts[0])


def test_judge_ok_keeps_every_repeated_growth_reason():
    reasons = [f"{name} adds the lines this reviewed request needs to answer one question"
               for name in ("first.py", "second.py", "third.py")]
    verb = judge.JudgeVerb.parse(["ok", "--pr", "8", "--head", "abc123def", "--why", ACCEPTED.text,
                            *(part for reason in reasons for part in ("--growth", reason))])
    growth = verb.head_operation().growth
    assert all(growth.names(f"src/{name}") for name in ("first.py", "second.py", "third.py"))
    assert not growth.names("src/fourth.py")
    with pytest.raises(ToolRefusedError, match="12\\+ words"):
        judge.JudgeVerb.parse(["ok", "--pr", "8", "--head", "abc123def", "--why", ACCEPTED.text,
                         "--growth", reasons[0], "--growth", "short.py grows"]).head_operation()


def test_an_armed_head_with_a_red_required_check_is_a_stuck_merge_every_pass(head_bus):
    """2026-10-25: #3332, red and conflicting, printed two UNSTICK lines; one head is one line
    naming every block on it, its age from the oldest."""
    comms, github, green = head_bus
    source_repo = green.repo
    stamp = datetime.fromtimestamp(time.time() - 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = datetime.fromtimestamp(time.time() - 600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    github.put(source_repo, UnmergedListedPullRequest(
        8, "fix/head-8", green.head, stamp, "main", reviewable_times=(stamp,),
        armed_times=(stamp, later),
        merge_blocks=(FailedCheckMergeBlock(stamp, "debt-src"), ConflictMergeBlock(later)),
    ))
    found, = StuckMerge.scan_pulls(comms.goals, ProgressRead(()))
    assert found.request.pr == 8
    assert found.line().endswith(
        ": auto-merge armed but required check debt-src failed; conflicts with its base"
        " -> fix debt-src at its owner; merge main into it")
    # The age is the heartbeat Ping's to render, from began(); the line names no clock.
    assert found.began() == found.blocks[0].since and found.broke_after(0)


def test_a_pr_verdict_acts_on_the_unit_holding_that_head(head_bus):
    """A verdict on a unit's PR named only the head, so a stale CLOSE line on that same unit
    refused the verdict the line was waiting for."""
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(comms.goals, todo.id)
    verb = judge.JudgeVerb.parse(["ok", "--pr", str(request.pr), "--head", request.head,
                                  "--why", ACCEPTED.text])
    ctx = order.Commander.read_root(comms.root)
    acted = frozenset(verb.subject(ctx).acts_on(ctx))
    assert acted == {HeadSubject(request), UnitSubject(held.owner_identity)}


def test_an_idle_draft_and_a_conflicting_ready_head_are_board_items(head_bus):
    """2026-10-25: five open agent-comms PRs, idle for hours or red, raised no line at all;
    then another commander's idle PRs on the same repository locked this one's verbs."""
    comms, github, ready = head_bus
    hour = datetime.fromtimestamp(time.time() - 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    github.put(ready.repo, DraftListedPullRequest(
        9, "fix/gone", "c" * 40, hour, "main", reviewable_times=(hour,)))
    github.put(ready.repo, DraftListedPullRequest(
        11, "fix/peer", "d" * 40, hour, "main", reviewable_times=(hour,)))
    github.put(ready.repo, UnmergedListedPullRequest(
        7, "fix/head-7", ready.head, fresh, "main", reviewable_times=(fresh,),
        merge_blocks=(ConflictMergeBlock(fresh),)))
    for number in (9, 7):
        todo = comms.goals.todos.get("P2")
        held = todo.held()
        RecordPullRequest(todo.revision, held.owner_identity,
                          PullRequestLink(ready.repo, number), held.generation).record(comms.goals, todo.id)
    ordered = (comms.goals.todos.get("P2"),)
    found = {rule.request.pr: rule.line()
             for rule in IdlePullRequest.scan_pulls(comms.goals, ProgressRead(()), (), ordered)}
    assert 11 not in found
    assert found[9].startswith(f"IDLE {ready.repo.to_wire()}#9 cccccccc")
    assert "(no live holder): head unmoved, idle 60 min -> land or supersede it" in found[9]
    assert "conflicts with its base, idle 0 min -> merge main into it" in found[7]
