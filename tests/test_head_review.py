"""Head decisions through the native boundary, real stores, and the external gh process."""

import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest


from agent_comms.commander import judge, order
from agent_comms.goal_actions import CreateProblemGoalAction
from commander_heartbeat_fixture import capture_frame
from agent_comms.head_review import HeadReview, PullRequestHead, ReadPullRequestHead
from agent_comms.heartbeat_subjects import CommanderHistory, HeadSubject
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import (
    HeadReviewTimerUp, IdlePullRequest, OrderlessPullRequest, OrderlessUnreviewedHead,
    LinkedUnreviewedHead, ReviewTimerUp, StuckMerge,
)
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.errors import ToolRefusedError
from agent_comms.nominal.field_codec import RecordProblems
from agent_comms.tool_caller import ThreadCaller
from agent_comms.work_branches import SourceGrowth, WorkBranches
from agent_comms.order_tools import CommsHeadVerdictTool
from agent_comms.todos import (
    CarriedHeadVerdict, ConflictMergeBlock, DraftListedPullRequest, FailedCheckMergeBlock, GrownHeadVerdict, GrowthReason, OrderHeadReview, LinkedHeadReview,
    NoHeadOrder, ProgressRead, PullRequestLink, RecordPullRequest, RejectedHeadVerdict,
    ReviewedHeadVerdict, StoredHeadReview, UnquotedHeadReview,
    StrikeReason, UnderReviewHeadVerdict, UnmergedListedPullRequest, VerdictReason,
)
from source_revision_fixture import SourceRepository

pytest_plugins = ("test_holder_standing_rules", "github_fixture")

REASON = StrikeReason("This head repeats the same decision in two unrelated places instead of using the existing class.")
ACCEPTED = VerdictReason("The existing class owns this behavior without another copy")


@pytest.mark.parametrize('roast,fields', (
    ('The decision could be improved because the same required checks are repeated across fields',
     {'why', 'roast.blunt'}),
    ('Maybe improve this', {'why', 'roast.words', 'roast.because', 'roast.blunt'}),
))
def test_back_collects_missing_and_invalid_fields(roast: str, fields: set[str]) -> None:
    with pytest.raises(RecordProblems) as refused:
        judge.JudgeVerb.parse([
            'back', '--pr', '1', '--head', 'a' * 40, '--at', 'owner.py:1',
            '--roast', roast,
            '--fix', 'Put the repeated checks on their existing declaring parent class',
        ])
    assert refused.value.problems.keys() >= fields


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


def test_integration_reads_latest_review_in_the_declared_context(github_cli, tmp_path):
    source = SourceRepository.create(tmp_path / 'status-source')
    github_cli.configure(source.github)
    request = PullRequestHead(source.github, source.number, source.branch_commit)
    assert not HeadReview.accepted(request.repo, request.head)
    ReviewedHeadVerdict(ACCEPTED).post(request.repo, request.head, 'lead', 'integration')
    assert HeadReview.accepted(request.repo, request.head)
    UnderReviewHeadVerdict().post(request.repo, request.head, 'lead', 'integration')
    assert not HeadReview.accepted(request.repo, request.head)
    ReviewedHeadVerdict(ACCEPTED).post(request.repo, request.head, 'lead', 'integration')
    assert HeadReview.accepted(request.repo, request.head)
    RejectedHeadVerdict(REASON).post(request.repo, request.head, 'lead', 'integration')
    assert not HeadReview.accepted(request.repo, request.head)
    assert not HeadReview.accepted(request.repo, 'a' * 40)


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
    assert StoredHeadReview.head_reviews(comms.goals.todos, request)[0].verdict == rejected
    current = ReadPullRequestHead(request.repo, request.pr, request.head, base="main")
    assert StoredHeadReview.head_reviews(comms.goals.todos, current) == StoredHeadReview.head_reviews(comms.goals.todos, request)
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
    assert isinstance(StoredHeadReview.head_reviews(comms.goals.todos, request)[0].verdict, ReviewedHeadVerdict)
    todo = comms.goals.todos.get("P2")
    location = f"{todo.order.check.directory / todo.order.owner.file}:{todo.order.owner.line}"
    judge.main(["back", *common,
                "--roast", "This is stupid because the same decision is written twice instead of belonging to its existing class.",
                "--fix", "Put the shared behavior on the existing parent and delete both copies.",
                "--slop", "IMPL-5", "--fact", "the same decision is written twice",
                "--at", location, "--derive", "base-method", "--by", location], context)
    assert isinstance(StoredHeadReview.head_reviews(comms.goals.todos, request)[0].verdict, RejectedHeadVerdict)
    assert len(tuple(call for call in github.calls() if f"statuses/{request.head}" in call[1])) == 2


def test_judge_ok_requests_merge_at_the_judged_head_unless_draft_and_fails_loudly(
        head_bus, reviewed_source, capsys):
    """2026-10-26: #3454 printed ok and stayed unarmed; #3444 needed a hand bypass-merge."""
    comms, github, request = head_bus
    for number in (8, 9):
        reviewed_source.git(reviewed_source.checkout, "push", "-q", "origin",
                            f"{request.head}:refs/pull/{number}/head")
    context = order.Commander.read_root(comms.root)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    github.put(request.repo, DraftListedPullRequest(
        8, "fix/draft", request.head, stamp, "main", reviewable_times=(stamp,)))
    github.put(request.repo, UnmergedListedPullRequest(
        9, "fix/refused", request.head, stamp, "main", reviewable_times=(stamp,)))
    github.refuse_arm(request.repo, 9, "Merge permission denied")

    def ok(number):
        judge.main(["ok", "--repo", request.repo.to_wire(), "--pr", str(number),
                    "--head", request.head, "--why", ACCEPTED.text], context)

    def arms(number):
        return [call for call in github.calls() if call[:3] == ("pr", "merge", str(number))]

    ok(request.pr)
    assert arms(request.pr) == [("pr", "merge", str(request.pr), "--repo", request.repo.to_wire(),
                                 "--auto", "--merge", "--match-head-commit", request.head)]
    assert f"#{request.pr} merge requested at {request.head}" in capsys.readouterr().out
    ok(8)
    assert not arms(8)
    assert "#8 is a draft, so not armed" in capsys.readouterr().out
    with pytest.raises(ToolRefusedError, match="Merge permission denied"):
        ok(9)
    assert "ok" not in capsys.readouterr().out
    assert isinstance(StoredHeadReview.head_reviews(
        comms.goals.todos, replace(request, pr=9))[0].verdict, ReviewedHeadVerdict)


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
    assert not StoredHeadReview.head_reviews(comms.goals.todos, request)
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
    assert not StoredHeadReview.head_reviews(comms.goals.todos, moved)
    assert not any("statuses/" in call[1] for call in github.calls())


def test_review_ok_follows_an_unchanged_change(head_bus, reviewed_source):
    comms, github, request = head_bus
    source = reviewed_source
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))

    (source.checkout / "base_only.py").write_text("advanced = True\n")
    source.git(source.checkout, "add", "base_only.py")
    source.git(source.checkout, "commit", "-m", "Advance the base independently")
    source.git(source.checkout, "push", "origin", "main")
    source.git(source.checkout, "checkout", "feature")
    source.git(source.checkout, "merge", "--no-edit", "main")
    merged = source.git(source.checkout, "rev-parse", "HEAD").strip()
    source.git(source.checkout, "push", "origin", "HEAD:refs/pull/7/head")
    moved = opened(github, source, request.pr, merged)

    pulls = ProgressRead()
    pulls.carry_reviews(comms.goals, request.repo)
    assert pulls.open(request.repo)[0].has_head_review
    assert StoredHeadReview.head_reviews(comms.goals.todos, moved)[0].verdict == CarriedHeadVerdict(ACCEPTED, request)
    assert not OrderlessUnreviewedHead.scan_reviews(comms.goals, "lead", pulls)
    carried_posts = tuple(call for call in github.calls() if f"statuses/{merged}" in call[1])
    assert len(carried_posts) == 1
    assert f"description=carried from {request.head[:9]}: same change" in carried_posts[0]

    owner = source.checkout / "owner.py"
    owner.write_text(owner.read_text() + 'spacing = "a b"\n')
    changed = source.publish_change("Add an unreviewed line", "owner.py")
    added = opened(github, source, request.pr, changed)
    pulls = ProgressRead()
    pulls.carry_reviews(comms.goals, request.repo)
    assert not pulls.open(request.repo)[0].has_head_review
    assert not StoredHeadReview.head_reviews(comms.goals.todos, added)
    assert not any(f"statuses/{changed}" in call[1] for call in github.calls())

    owner.write_text(owner.read_text() + (
        "\ndef evaluate(flag):\n    if flag:\n        print(flag)\n"
        "        return 2\n    return 1\n"))
    eight = opened(github, source, request.pr,
                   source.publish_change("Review an eight-space return", "owner.py"))
    invoke(comms, eight, ReviewedHeadVerdict(ACCEPTED))
    owner.write_text(owner.read_text().replace("        return 2\n", "    return 2\n"))
    four = opened(github, source, request.pr,
                  source.publish_change("Move the return outside the branch", "owner.py"))
    reader = WorkBranches(source.checkout)
    reader.prepare_pull_request("main", eight.head)
    reader.prepare_pull_request("main", four.head)
    assert eight.change_identity(reader, "main") != four.change_identity(reader, "main")
    pulls = ProgressRead()
    pulls.carry_reviews(comms.goals, request.repo)
    assert not pulls.open(request.repo)[0].has_head_review
    assert not StoredHeadReview.head_reviews(comms.goals.todos, four)
    assert not any(f"statuses/{four.head}" in call[1] for call in github.calls())


def test_overdue_linked_head_stays_visible_without_refusing_new_orders(head_bus):
    """An overdue source review must not prevent an independent recovery order."""
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    action = CreateProblemGoalAction(
        problem="P3", commander="lead", request=todo.request,
        assigner=comms.registry.require("lead").incarnation,
        repo=todo.repo, text="Repair the next owner", order=todo.order.checked(),
        review_at=RecordedEventTime(time.time() + 3600),
    )
    assert [rule.request for rule in OrderlessUnreviewedHead.scan_reviews(
        comms.goals, "lead", ProgressRead(()))] == [request]
    comms.goals.update_goal("outsider", action)
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(comms.goals, todo.id)
    held_orders = comms.goals.todos.held_for_commander(
        comms.registry.require("lead").incarnation)
    assert [rule.request for rule in LinkedUnreviewedHead.scan_reviews(
        comms.goals, "lead", ProgressRead(held_orders))] == [request]
    comms.goals.update_goal("outsider", replace(action, problem="P4"))
    assert comms.goals.todos.get("P4").held().owner == "outsider"


@pytest.mark.parametrize("has_head", (False, True))
def test_review_timer_up_with_open_head_builds(held_bus, github_cli, reviewed_source, has_head):
    """One registry scan builds one timer line, with a head only when the forge has one."""
    comms = held_bus
    original = comms.goals.todos.get("P2")
    comms.goals.update_goal("worker", CreateProblemGoalAction(
        problem="P3", commander="lead", request=original.request,
        assigner=comms.registry.require("lead").incarnation,
        repo=original.repo, text="Repair the overdue order", order=original.order.checked(),
        review_at=RecordedEventTime(time.time() - 1),
    ))
    todo = comms.goals.todos.get("P3")
    if has_head:
        request = opened(github_cli, reviewed_source, 7, reviewed_source.branch_commit)
        held = todo.held()
        RecordPullRequest(todo.revision, held.owner_identity,
                          PullRequestLink(request.repo, request.pr), held.generation).record(
                              comms.goals, todo.id)
    frame = capture_frame(comms, "lead", ProgressRead(()))
    assert HeadReviewTimerUp.scan_owner() is ReviewTimerUp
    found, = ReviewTimerUp.broken(comms, "lead", observed=(), frame=frame)
    assert found.problem == "P3"
    assert found.holder == todo.held().owner_identity
    assert found.order == todo.order.checked().id
    assert found.began() <= time.time()
    if has_head:
        assert type(found) is HeadReviewTimerUp
        assert found.request == request
    else:
        assert type(found) is ReviewTimerUp
    assert "P3" in found.line()
    assert "worker" in found.line()


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
    assert not subject.disposed(CommanderHistory.read(comms, frozenset()), since)
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    assert owed() == []
    assert subject.disposed(CommanderHistory.read(comms, frozenset()), since)
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
    assert StoredHeadReview.head_reviews(comms.goals.todos, request)[0].order.problem == todo.id


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
    assert not StoredHeadReview.head_reviews(comms.goals.todos, request)
    invoke(comms, request, GrownHeadVerdict(ACCEPTED, GrowthReason(
        "grown.py declares forty one values that the reviewed request needs as separate names")))
    posts = tuple(call for call in github.calls() if f"statuses/{head}" in call[1])
    assert len(posts) == 1 and any(f"(src +{grown})" in part for part in posts[0])

    # The actual CLI must report both independent failures, then infer the unique
    # order when corrected. Native orderless acceptance above remains legal.
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(
                          comms.goals, todo.id)
    before = comms.goals.todos.get(todo.id)
    common = ["judge", "ok", "--repo", request.repo.to_wire(), "--pr", str(request.pr),
              "--head", head, "--why", ACCEPTED.text]

    def cli(*arguments):
        return subprocess.run(
            [sys.executable, str(Path(sys.executable).with_name("agent-comms-commander")),
             "--root", str(comms.root), *arguments],
            cwd=comms.root, capture_output=True, text=True, timeout=30,
        )

    refused = cli(*common)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "--serves" in refused.stderr and f"src/grown.py +{grown}" in refused.stderr
    assert comms.goals.todos.get(todo.id) == before
    assert len(StoredHeadReview.head_reviews(comms.goals.todos, request)) == 1

    back = cli("judge", "back", "--repo", request.repo.to_wire(), "--pr", str(request.pr),
               "--head", head, "--at", "owner.py:2",
               "--roast", "The decision could be improved because it repeats the required checks across fields",
               "--fix", "Put the repeated checks on their existing declaring parent class")
    assert back.returncode == 1, back.stdout + back.stderr
    assert "why" in back.stderr and "roast.blunt" in back.stderr
    assert comms.goals.todos.get(todo.id) == before

    accepted = cli(*common, "--serves", before.order.checked().edits[0], "--growth",
                   "grown.py declares forty one values that the reviewed request needs as separate names")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert StoredHeadReview.head_reviews(comms.goals.todos, request)[0].order.problem == todo.id


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
        " has auto-merge armed but required check debt-src failed; conflicts with its base."
        " who's fixing it? -> fix debt-src at its owner; merge main into it")
    # The age is the heartbeat Ping's to render, from began(); the line names no clock.
    assert found.began() == found.blocks[0].since and found.broke_after(0)


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
    assert found[9].startswith(f"IDLE {ready.repo.to_wire()}#9 {'c' * 9} (no live holder)")
    assert "idle 60 min" in found[9] and found[9].endswith(" -> land or supersede it")
    assert "idle 0 min" in found[7] and found[7].endswith(" -> merge main into it")


def working_on(comms, source, tmp_path, name, branch):
    """A registered thread whose own worktree has ``branch`` checked out."""
    worktree = tmp_path / f"{name}-checkout"
    source.git(source.checkout, "worktree", "add", "-q", "-b", branch, str(worktree))
    comms.registry.declare(Thread(name, frozenset(), Checkout(worktree)))


def test_a_request_no_order_links_reaches_the_thread_working_on_its_branch(
        head_bus, reviewed_source, tmp_path):
    """2026-10-27: agent-comms#3535 sat BLOCKED on red debt checks with auto-merge armed and
    no order linking it. No IDLE line could see it and its UNSTICK line named nobody, so its
    author learned only when Tristan asked. Work outside the order system is on the board,
    naming whoever has its branch checked out; a request an order links stays that order's."""
    comms, github, ready = head_bus
    hour = datetime.fromtimestamp(time.time() - 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    working_on(comms, reviewed_source, tmp_path, "author", "fix/head-9")
    working_on(comms, reviewed_source, tmp_path, "armer", "fix/armed")
    github.put(ready.repo, UnmergedListedPullRequest(
        9, "fix/head-9", "e" * 40, hour, "main", reviewable_times=(hour,),
        merge_blocks=(FailedCheckMergeBlock(hour, "debt-src"),)))
    github.put(ready.repo, DraftListedPullRequest(
        11, "fix/nobody", "f" * 40, hour, "main", reviewable_times=(hour,)))
    github.put(ready.repo, UnmergedListedPullRequest(
        12, "fix/armed", "a" * 40, hour, "main", reviewable_times=(hour,), armed_times=(hour,),
        merge_blocks=(FailedCheckMergeBlock(hour, "debt-src"),)))
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity, PullRequestLink(ready.repo, ready.pr),
                      held.generation).record(comms.goals, todo.id)
    found = {rule.request.pr: rule.line()
             for rule in OrderlessPullRequest.scan_orderless(comms.goals, ProgressRead(()))}
    # The ordered request is its order's line; the armed one is StuckMerge's.
    assert set(found) == {9, 11}
    assert found[9].startswith(
        f"UNORDERED {ready.repo.to_wire()}#9 {'e' * 9} (author): required check debt-src failed")
    assert found[9].endswith(" -> fix debt-src at its owner; order author")
    assert found[11].startswith(
        f"UNORDERED {ready.repo.to_wire()}#11 {'f' * 9} (no order, and no registered thread has"
        " its branch checked out): head unmoved")
    assert found[11].endswith(" -> order its author or close it with a reason")
    stuck, = StuckMerge.scan_pulls(comms.goals, ProgressRead(()))
    assert stuck.request.pr == 12
    assert stuck.line().endswith(" -> fix debt-src at its owner at armer")


def test_another_commanders_linked_request_stays_off_the_orderless_board(head_bus):
    """The 2026-10-25 guarantee holds: a request an open order links is never orderless.
    The orders read are every commander's open orders, so another commander's linked
    request is its own board's line, never this one's."""
    comms, github, ready = head_bus
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity, PullRequestLink(ready.repo, ready.pr),
                      held.generation).record(comms.goals, todo.id)
    assert ready.pr not in {rule.request.pr for rule in
                            OrderlessPullRequest.scan_orderless(comms.goals, ProgressRead(()))}
