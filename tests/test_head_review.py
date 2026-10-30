"""Head decisions through the native boundary, real stores, and the external gh process."""

import asyncio
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import chdir, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest


from agent_comms.commander import judge, order
from agent_comms.comms import Comms
from agent_comms.closed_pull_requests import ClosedPullRequestWatch
from agent_comms.commander.publication_rules import ClosedUnmergedPullRequest
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.build_identity import RepositoryCommit
from agent_comms.head_review import (
    DraftMergeArm, HeadReview, IntegrationInput, LocalIntegrationSource, MergedIntegrationSource, MergeHeadReview, OrdinaryInput, PromotedHeadReview, PullRequestHead, ReadPullRequestHead,
)
from agent_comms.integration import IntegrationRun
from commander_heartbeat_fixture import capture_frame
from agent_comms.heartbeat_subjects import CommanderHistory, HeadSubject, UnlandedHeadSubject
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import (
    HeadReviewTimerUp, IdlePullRequest, OrderlessPullRequest, OrderlessUnreviewedHead,
    LinkedUnreviewedHead, ReviewTimerUp, StuckMerge,
)
from agent_comms.repo import Checkout
from agent_comms.task_attachments.commander_pings import CommanderDecisions, CommanderUpdate, CommanderUpdateStore, RulePing
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.errors import TodoError
from agent_comms.nominal.field_codec import FieldCodec, GoalId, MalformedRecord, RecordProblems
from agent_comms.tool_caller import ThreadCaller
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.work_branches import (
    SourceGrowth, ThreadBranchCheckout, UnregisteredBranchCheckout, WorkBranches,
)
from agent_comms.order_tools import CommsHeadVerdictTool
from agent_comms.todos import (
    CarriedHeadVerdict, ClosedListedPullRequest, ConflictMergeBlock, DraftListedPullRequest, FailedCheckMergeBlock, GrownHeadVerdict, GrowthReason, OrderHeadReview, LinkedHeadReview,
    NoHeadOrder, OrderClaim, ProgressRead, PullRequestLink, PullRequests, RecordPullRequest, RejectedHeadVerdict,
    HeadVerdict, HeadReviewed, QualifiedHeadVerdict, ReviewedHeadVerdict, StoredHeadReview, UnquotedHeadReview,
    AbandonedHeadVerdict, StrikeReason, SupersededHeadVerdict, UnderReviewHeadVerdict, UnmergedListedPullRequest, VerdictReason, WithdrawTodo,
)
from agent_comms_hooks import ToolRefusedError
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources
from source_revision_fixture import SourceRepository

pytest_plugins = ("test_holder_standing_rules", "github_fixture", "integration_source_fixture")

REASON = StrikeReason("This head repeats the same decision in two unrelated places instead of using the existing class.")
ACCEPTED = VerdictReason("The existing class owns this behavior without another copy")
QUALIFIED = VerdictReason("The source stage is reviewed while paired implementation remains incomplete")


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


@pytest.fixture
def linked_head_bus(head_bus):
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(comms.goals, todo.id)
    return head_bus


def invoke(comms, request, verdict, order=NoHeadOrder()):
    tool = CommsHeadVerdictTool(request=request, operation=verdict, order=order)
    return tool.invoke(comms, tool.arguments, caller=ThreadCaller("lead"))


def promote(context: order.CommanderContext, request: PullRequestHead) -> HeadReviewed:
    return judge.PromoteVerb(pr=request.pr, head=request.head, repo=request.repo.to_wire(),
                             why=ACCEPTED.text).review_head(context, request, NoHeadOrder())


def review_posts(github: GitHubFixture, head: str) -> tuple[tuple[str, ...], ...]:
    """Count source decisions independently of their derived merge withdrawal."""
    return tuple(call for call in github.calls() if f"statuses/{head}" in call[1]
                 and f"context={HeadReview.context}" in call)


def test_qualification_records_each_open_order_cut_without_promoting_or_finishing_work(
        linked_head_bus, claim_order):
    """A ready companion's source stage is a decision, not permission to ship its incomplete pair."""
    comms, github, request = linked_head_bus
    context = order.Commander.read_root(comms.root)

    def qualify_unchanged():
        before = comms.goals.todos.get("P2")
        goal = comms.registry.require("worker").goal.require()
        quote = before.order.checked().edits[0]
        judge.main(["qualify", "--repo", request.repo.to_wire(), "--pr", str(request.pr),
                    "--head", request.head, "--why", QUALIFIED.text, "--serves", quote], context)
        assert comms.goals.todos.get("P2") == replace(before, revision=before.revision + 1)
        assert comms.registry.require("worker").goal.require() == replace(goal, revision=goal.revision + 1)
        record = StoredHeadReview.head_reviews(comms.goals.todos, request)[0]
        assert record.verdict == QualifiedHeadVerdict(QUALIFIED)
        assert record.order == LinkedHeadReview.of(before, quote)
        assert record.verdict.decides()
        assert record.verdict.settles_edits(record, 0) == record.at.recorded_at("qualified source cut")
        assert not record.verdict.allows_integration()
        assert not HeadReview.accepted(request.repo, request.head)
        with pytest.raises(TodoError, match="no recorded ok verdict"):
            record.verdict.require_merge(request, context.repos)

    qualify_unchanged()  # Pending: the order still requires every unfinished clause.
    claim_order("P2", pull_request=request.pr, head=request.head)
    qualify_unchanged()  # Claimed: the delivery is neither accepted nor completed.
    todo = comms.goals.todos.get("P2")
    invoke(comms, request, UnderReviewHeadVerdict(), OrderHeadReview.for_request(
        (todo,), request, UnquotedHeadReview.of))
    qualify_unchanged()  # Reviewing: the recorded source cut changes no held work.
    posts = review_posts(github, request.head)
    assert len(posts) == 7
    assert all("state=pending" in call for call in posts)
    assert not any(call[:2] == ("pr", "merge") for call in github.calls())


@pytest.fixture
def companion_checkout(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead],
    reviewed_source: SourceRepository,
    configured_repository: Callable[[Comms, *tuple[SourceRepository, ...]], order.Commander],
    tmp_path: Path,
) -> Path:
    comms, github, primary = linked_head_bus
    companion = SourceRepository.create(tmp_path / "companion", RepositoryName("example", "companion"))
    configured_repository(comms, reviewed_source, companion)
    stamp = "2026-10-24T00:00:00Z"
    # The same PR number exists in both repositories: substituting todo.repo cannot pass.
    github.put(companion.github, DraftListedPullRequest(
        primary.pr, "feature", companion.branch_commit, stamp, "main", reviewable_times=(stamp,)))
    owner = comms.registry.require(comms.goals.todos.get("P2").held().owner)
    rule = comms.registry.storage_rule
    checkout = rule.repository_checkout(rule.roots_of(owner), "P2", companion.github)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    companion.git(companion.checkout, "worktree", "add", "--detach", str(checkout), companion.branch_commit)
    return checkout


def test_native_companion_link_preserves_claim_review_goal_and_is_idempotent(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead],
    companion_checkout: Path,
    claim_order: Callable[..., OrderClaim],
) -> None:
    comms, github, primary = linked_head_bus
    claim_order("P2", pull_request=primary.pr, head=primary.head)
    todo = comms.goals.todos.get("P2")
    invoke(comms, primary, UnderReviewHeadVerdict(), OrderHeadReview.for_request(
        (todo,), primary, UnquotedHeadReview.of))
    before = comms.goals.todos.get("P2")
    goal = comms.registry.require(before.held().owner).goal
    reviews = StoredHeadReview.head_reviews(comms.goals.todos, primary)
    history = tuple(comms.registry.goal_history(before.held().owner))
    link = PullRequestLink(SourceRepository.of(companion_checkout).github, primary.pr)
    context = order.Commander.read_root(comms.root)
    with chdir(companion_checkout):
        order.main(["pr", before.id, "--existing", str(primary.pr)], context)
    after = comms.goals.todos.get(before.id)
    assert after == replace(before, revision=before.revision + 1, order=before.order.checked().linked_to(link))
    assert type(after.order) is type(before.order)
    assert comms.registry.require(after.held().owner).goal == goal
    assert StoredHeadReview.head_reviews(comms.goals.todos, primary) == reviews
    companion = PullRequestHead(link.repo, link.number, SourceRepository.git(companion_checkout, "rev-parse", "HEAD").strip())
    assert PullRequests.linked_among((after,), link.repo) == {link.number: [after]}
    assert PullRequests((after,)).linked(link.repo) == {link.number: [after]}
    assert OrderHeadReview.holding((after,), companion) == (after,)
    # Qualification reads these exact stored links, not a caller-provided pair roster.
    assert {item.repo for item in after.order.checked().pull_requests} == {primary.repo, link.repo}
    story = after.story()
    with chdir(companion_checkout):
        order.main(["pr", before.id, "--existing", str(primary.pr)], context)
    assert comms.goals.todos.get(before.id) == after
    assert comms.goals.todos.get(before.id).story() == story
    assert comms.registry.require(after.held().owner).goal == goal
    assert StoredHeadReview.head_reviews(comms.goals.todos, primary) == reviews
    assert tuple(comms.registry.goal_history(after.held().owner)) == history
    assert not any(call[:2] in (("pr", "create"), ("pr", "merge")) for call in github.calls())
    assert any(link.repo.to_wire() in argument for call in github.calls() for argument in call)


def test_native_companion_adoption_is_the_same_reviewed_orders_actual_qualification_source(
    source_pair: MutualSources, claim_order: Callable[..., OrderClaim],
    github_cli: GitHubFixture,
) -> None:
    pair = source_pair
    comms = pair.context.comms
    primary = PullRequestHead(pair.core.github, pair.core.number, pair.core.branch_commit)
    claim_order("P2", pull_request=primary.pr, head=primary.head)
    todo = comms.goals.todos.get("P2")
    invoke(comms, primary, UnderReviewHeadVerdict(), OrderHeadReview.for_request(
        (todo,), primary, UnquotedHeadReview.of))
    before = comms.goals.todos.get("P2")
    owner = comms.registry.require(before.held().owner)
    rule = comms.registry.storage_rule
    checkout = rule.repository_checkout(rule.roots_of(owner), before.id, pair.client.github)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    pair.client.git(pair.client.checkout, "worktree", "add", "--detach", str(checkout), pair.client.branch_commit)
    with chdir(checkout):
        order.main(["pr", before.id, "--existing", str(pair.client.number)], order.Commander.read_root(comms.root))
    linked = comms.goals.todos.get(before.id)
    assert linked == replace(before, revision=before.revision + 1,
                             order=before.order.checked().linked_to(pair.client.link))
    inputs = pair.capture(("P2",), "native qualified companion discovery")
    assert inputs.source(pair.client.github).heads == (
        PullRequestHead(pair.client.github, pair.client.number, pair.client.branch_commit),)
    assert comms.goals.todos.get(before.id) == linked
    assert not HeadReview.accepted(primary.repo, primary.head)
    assert not any(call[:2] in (("pr", "create"), ("pr", "merge")) for call in github_cli.calls())


def test_native_companion_creation_records_its_owned_checkout_repository_not_the_primary(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
    tmp_path: Path,
) -> None:
    comms, github, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    companion = SourceRepository.of(companion_checkout)
    number = primary.pr + 1
    stamp = "2026-10-24T00:00:00Z"
    github.create_next(companion.github, DraftListedPullRequest(
        number, "companion-feature", companion.git(companion_checkout, "rev-parse", "HEAD").strip(),
        stamp, "main", reviewable_times=(stamp,)))
    body = tmp_path / "companion-body.md"
    body.write_text("The actual paired source belongs to this existing order.\n")
    with chdir(companion_checkout):
        order.main(["pr", before.id, "--title", "Migrate the paired source",
                    "--body-file", str(body)], order.Commander.read_root(comms.root))
    after = comms.goals.todos.get(before.id)
    link = PullRequestLink(companion.github, number)
    assert after == replace(before, revision=before.revision + 1,
                            order=before.order.checked().linked_to(link))
    assert after.repo == before.repo == primary.repo != companion.github
    assert PullRequests.linked_among((after,), companion.github) == {number: [after]}
    created = tuple(call for call in github.calls() if call[:2] == ("pr", "create"))
    assert created == (("pr", "create", "--draft", "-R", companion.github.to_wire(),
                        "--title", "Migrate the paired source", "--body-file", "-"),)


def test_companion_adoption_cannot_edit_or_adopt_missing_pr(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
) -> None:
    comms, github, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    context = order.Commander.read_root(comms.root)
    with chdir(companion_checkout):
        for arguments in (
            ["--existing", str(primary.pr), "--title", "Do not edit"],
            ["--existing", "999999"],
        ):
            with pytest.raises((ValueError, TodoError, SystemExit, subprocess.CalledProcessError)):
                order.main(["pr", before.id, *arguments], context)
            assert comms.goals.todos.get(before.id) == before
    assert not any(call[:2] in (("pr", "create"), ("pr", "edit"), ("pr", "merge")) for call in github.calls())


def test_companion_metadata_refuses_an_existing_but_inaccessible_remote_pr(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
) -> None:
    comms, github, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    companion = SourceRepository.of(companion_checkout)
    github.refuse_read(companion.github, primary.pr, "GraphQL: Resource not accessible by integration")
    with chdir(companion_checkout), pytest.raises(subprocess.CalledProcessError):
        order.main(["pr", before.id, "--existing", str(primary.pr)], order.Commander.read_root(comms.root))
    assert comms.goals.todos.get(before.id) == before
    assert not any(call[:2] in (("pr", "create"), ("pr", "edit")) for call in github.calls())


def test_companion_metadata_refuses_another_owners_checkout(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
) -> None:
    comms, github, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    companion = SourceRepository.of(companion_checkout)
    other = comms.registry.require("lead")
    rule = comms.registry.storage_rule
    checkout = rule.repository_checkout(rule.roots_of(other), "P2", companion.github)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    companion.git(companion_checkout, "worktree", "add", "--detach", str(checkout), "HEAD")
    calls = github.calls()
    with chdir(checkout), pytest.raises(ValueError, match="owned by"):
        order.main(["pr", before.id, "--existing", str(primary.pr)], order.Commander.read_root(comms.root))
    assert comms.goals.todos.get(before.id) == before
    assert github.calls() == calls


def test_companion_metadata_refuses_an_unregistered_origin_before_forge_read(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
    reviewed_source: SourceRepository,
    configured_repository: Callable[[Comms, *tuple[SourceRepository, ...]], order.Commander],
) -> None:
    comms, github, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    context = configured_repository(comms, reviewed_source)
    calls = github.calls()
    with chdir(companion_checkout), pytest.raises(ValueError, match="Unknown repository"):
        order.main(["pr", before.id, "--existing", str(primary.pr)], context)
    assert comms.goals.todos.get(before.id) == before
    assert github.calls() == calls


def test_companion_metadata_refuses_a_closed_order_without_forge_or_link_changes(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
) -> None:
    comms, github, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    comms.goals.todos.apply(before.id, WithdrawTodo(
        before.revision, comms.registry.require("lead").incarnation,
        before.held(), "Close this fixture order before attempting metadata"))
    closed = comms.goals.todos.get(before.id)
    calls = github.calls()
    with chdir(companion_checkout), pytest.raises((ValueError, TodoError)):
        order.main(["pr", before.id, "--existing", str(primary.pr)], order.Commander.read_root(comms.root))
    assert comms.goals.todos.get(before.id) == closed
    assert github.calls() == calls


def test_companion_metadata_write_enforces_registration_revision_and_holder(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
) -> None:
    comms, _, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    held = before.held()
    worker = held.owner_identity
    registered = PullRequestLink(SourceRepository.of(companion_checkout).github, primary.pr)
    for command in (
        RecordPullRequest(before.revision, worker, PullRequestLink(RepositoryName("unknown", "source"), 7), held.generation),
        RecordPullRequest(before.revision + 1, worker, registered, held.generation),
        RecordPullRequest(before.revision, worker, registered, GoalId.mint()),
        RecordPullRequest(before.revision, RecordedThreadIncarnation("not-holder", 1.0),
                          registered, held.generation),
    ):
        with pytest.raises((ValueError, TodoError)):
            comms.goals.todos.apply(before.id, command)
        assert comms.goals.todos.get(before.id) == before


def test_qualification_withdraws_acceptance_without_claiming_to_disarm_the_request(head_bus):
    comms, github, request = head_bus
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    arms = tuple(call for call in github.calls() if call[:2] == ("pr", "merge"))
    assert arms == () and HeadReview.accepted(request.repo, request.head)
    with pytest.raises(ToolRefusedError, match="captured combined carrier"):
        ReviewedHeadVerdict.require_merge(request, order.Commander.read_root(comms.root).repos)
    invoke(comms, request, QualifiedHeadVerdict(QUALIFIED))
    assert StoredHeadReview.current_verdict(comms.goals.todos, request) is QualifiedHeadVerdict
    assert not HeadReview.accepted(request.repo, request.head)
    assert tuple(call for call in github.calls() if call[:2] == ("pr", "merge")) == arms
    posts = review_posts(github, request.head)
    assert "state=pending" in posts[-1]
    # Reaccepting this source permits combination, never an individual merge.
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    assert HeadReview.accepted(request.repo, request.head)
    assert not any(call[:2] == ("pr", "merge") for call in github.calls())


def test_qualification_codec_and_head_only_cli_require_a_reason():
    verdict = QualifiedHeadVerdict(QUALIFIED)
    assert FieldCodec.decode(HeadVerdict, FieldCodec.encode(verdict, HeadVerdict)) == verdict
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(HeadVerdict, {"kind": FieldCodec.encode(QualifiedHeadVerdict)})
    common = ["qualify", "--pr", "7", "--head", "a" * 40]
    assert judge.JudgeVerb.parse([*common, "--why", QUALIFIED.text]).head_operation() == verdict
    with pytest.raises(RecordProblems) as refused:
        judge.JudgeVerb.parse(common)
    assert "why" in refused.value.problems
    with pytest.raises(RecordProblems) as refused:
        judge.JudgeVerb.parse(["qualify", "E1:abcd", "--why", QUALIFIED.text,
                              "--serves", "Repair the existing owner"])
    assert "ids" in refused.value.problems


@pytest.mark.parametrize("quote", ("", "These words never occur in the held order"))
def test_linked_qualification_requires_the_original_order_quote(linked_head_bus, quote):
    comms, github, request = linked_head_bus
    before = comms.goals.todos.get("P2")
    with pytest.raises(ToolRefusedError):
        judge.main(["qualify", "--repo", request.repo.to_wire(), "--pr", str(request.pr),
                    "--head", request.head, "--why", QUALIFIED.text, "--serves", quote],
                   order.Commander.read_root(comms.root))
    assert comms.goals.todos.get(before.id) == before
    assert not StoredHeadReview.head_reviews(comms.goals.todos, request)
    assert not any("statuses/" in call[1] for call in github.calls())


def test_qualification_records_an_untagged_peer_without_transferring_order_ownership(linked_head_bus):
    comms, github, request = linked_head_bus
    before = comms.goals.todos.get("P2")
    tool = CommsHeadVerdictTool(
        request=request, operation=QualifiedHeadVerdict(QUALIFIED),
        order=OrderHeadReview.for_request((before,), request,
                                        lambda linked: LinkedHeadReview.of(linked, linked.order.edits[0])))
    tool.invoke(comms, tool.arguments, caller=ThreadCaller("worker"))
    recorded, = StoredHeadReview.head_reviews(comms.goals.todos, request)
    assert recorded.actor.name == "worker"
    assert recorded.actor.created_at == comms.registry.require("worker").incarnation.created_at
    after = comms.goals.todos.get(before.id)
    assert after.creator == before.creator
    assert after.holder_state.previous_assignment() == before.holder_state.previous_assignment()
    assert any("statuses/" in call[1] for call in github.calls())


def test_qualification_does_not_carry_acceptance_to_an_unchanged_later_head(head_bus, reviewed_source):
    comms, github, request = head_bus
    source = reviewed_source
    invoke(comms, request, QualifiedHeadVerdict(QUALIFIED))
    (source.checkout / "base_only.py").write_text("advanced = True\n")
    source.git(source.checkout, "add", "base_only.py")
    source.git(source.checkout, "commit", "-m", "Advance the base independently")
    source.git(source.checkout, "push", "origin", "main")
    source.git(source.checkout, "checkout", "feature")
    source.git(source.checkout, "merge", "--no-edit", "main")
    head = source.git(source.checkout, "rev-parse", "HEAD").strip()
    source.git(source.checkout, "push", "origin", "HEAD:refs/pull/7/head")
    moved = opened(github, source, request.pr, head)
    branches = WorkBranches(source.checkout)
    branches.prepare_pull_request("main", request.head)
    branches.prepare_pull_request("main", moved.head)
    assert request.change_identity(branches, "main") == moved.change_identity(branches, "main")
    pulls = ProgressRead()
    pulls.carry_reviews(comms.goals, request.repo)
    assert not StoredHeadReview.head_reviews(comms.goals.todos, moved)
    assert not pulls.open(request.repo)[0].has_head_review
    assert not any(f"statuses/{head}" in call[1] for call in github.calls())


@pytest.fixture
def captured_input(reviewed_source: SourceRepository) -> IntegrationInput:
    source = reviewed_source
    return OrdinaryInput((LocalIntegrationSource(
        RepositoryCommit(source.github, source.main_commit),
        (PullRequestHead(source.github, source.number, source.branch_commit),),
        source.git(source.checkout, "rev-parse", f"{source.branch_commit}^{{tree}}").strip(),
    ),))


def test_integration_metadata_is_canonical_and_ordinary_messages_have_no_digest(
        captured_input: IntegrationInput) -> None:
    inputs = captured_input
    assert FieldCodec.decode(IntegrationInput, FieldCodec.parse(inputs.canonical)) == inputs
    assert inputs.local is inputs.sources[0]
    assert inputs.companions == ()
    message = f"Combine the real source changes\n\n{inputs.trailer}\n"
    assert IntegrationInput.from_message(message) == (inputs,)
    assert IntegrationInput.digest_from_message(message) == inputs.digest
    assert IntegrationInput.from_message("An ordinary substantive change\n") == ()
    assert IntegrationInput.digest_from_message("An ordinary substantive change\n") == ""


@pytest.mark.parametrize("damage", (
    lambda trailer: trailer + "\n" + trailer,
    lambda trailer: trailer.replace(": ", ":\t", 1),
    lambda trailer: trailer.replace('"sources":', '"sources": ', 1),
    lambda trailer: trailer + "\nUnrelated text after the trailer",
    lambda trailer: trailer + "garbage",
))
def test_attempted_integration_metadata_never_falls_back_to_ordinary_mode(
        captured_input: IntegrationInput, damage: Callable[[str], str]) -> None:
    with pytest.raises(MalformedRecord):
        IntegrationInput.from_message(damage(captured_input.trailer))


def test_integration_metadata_refuses_duplicate_json_fields_and_unknown_shapes(
        captured_input: IntegrationInput) -> None:
    canonical = captured_input.canonical
    duplicate = canonical[:-1] + "," + canonical[1:]
    for payload in (duplicate, '{"sources":[],"unsupported_version":2}'):
        with pytest.raises(MalformedRecord):
            IntegrationInput.from_message(f"{IntegrationInput.trailer_name}: {payload}")


def test_integration_source_refuses_duplicates_abbreviations_and_foreign_members(
        captured_input: IntegrationInput) -> None:
    source, = captured_input.sources
    head, = source.heads
    with pytest.raises(MalformedRecord):
        replace(source, heads=(head, head))
    with pytest.raises(MalformedRecord):
        replace(source, baseline=replace(source.baseline, commit=source.baseline.commit[:9]))
    with pytest.raises(MalformedRecord):
        replace(source, heads=(replace(head, head=head.head[:9]),))
    with pytest.raises(MalformedRecord):
        replace(source, heads=(replace(head, repo=RepositoryName.from_wire("other/repository")),))
    with pytest.raises(MalformedRecord):
        OrdinaryInput((source, source))
    with pytest.raises(ToolRefusedError):
        captured_input.require_repositories((source.repository, RepositoryName.from_wire("other/repository")))


def test_companion_input_changes_the_identity_of_an_unchanged_local_patch(
        captured_input: IntegrationInput, reviewed_source: SourceRepository, tmp_path: Path,
        github_cli: GitHubFixture) -> None:
    source = reviewed_source
    companion = SourceRepository.create(tmp_path / "companion", RepositoryName.from_wire("example/client"))
    client = MergedIntegrationSource(
        RepositoryCommit(companion.github, companion.main_commit),
        (PullRequestHead(companion.github, companion.number, companion.branch_commit),),
        companion.git(companion.checkout, "rev-parse", f"{companion.branch_commit}^{{tree}}").strip(),
    )
    inputs = replace(captured_input, sources=tuple(sorted((*captured_input.sources, client), key=lambda row: row.repository.key)))
    assert inputs.local is captured_input.local
    assert inputs.companions == (client,)
    companion.git(companion.checkout, "checkout", "feature")
    owner = companion.checkout / "owner.py"
    owner.write_text(owner.read_text() + "new_export = True\n")
    moved = companion.publish_change("Change the paired client", "owner.py")
    changed = replace(client, heads=(replace(client.heads[0], head=moved),),
                      candidate_tree=companion.git(companion.checkout, "rev-parse", f"{moved}^{{tree}}").strip())
    updated = replace(inputs, sources=tuple(changed if row == client else row for row in inputs.sources))
    core, = captured_input.sources
    heads = tuple(PullRequestHead(source.github, source.number, source.git(
        source.checkout, "commit-tree", core.candidate_tree,
        "-p", source.main_commit, "-p", source.branch_commit,
        "-m", f"Combine the substantive Core source\n\n{snapshot.trailer}",
    ).strip()) for snapshot in (inputs, updated))
    reader = WorkBranches(source.checkout)
    for head in heads:
        reader.prepare_pull_request("main", head.head)
    before, after = (head.change_identity(reader, "main") for head in heads)
    assert before.patch == after.patch
    assert before.integration_digest == inputs.digest
    assert after.integration_digest == updated.digest
    assert before != after
    # An author's canonical self-hash is source identity, never an accepted review.
    github_cli.configure(source.github)
    assert not any(HeadReview.accepted(head.repo, head.head) for head in heads)


def test_pending_sources_can_compute_but_only_reviewed_other_heads_allow_carrier_promotion(
        mutual_sources: MutualSources, github_cli: GitHubFixture) -> None:
    pair = mutual_sources
    comms = pair.context.comms
    client = PullRequestHead(pair.client.github, pair.client.number, pair.client.branch_commit)
    invoke(comms, client, UnderReviewHeadVerdict())
    run = IntegrationRun(pair.checkout, pair.output, "pending inputs compute without promotion")
    inputs = pair.capture(("P2", "P3"), run.risk)
    carrier = pair.publish_carrier(github_cli)
    invoke(comms, carrier, UnderReviewHeadVerdict())
    for verdict in (UnderReviewHeadVerdict(), QualifiedHeadVerdict(QUALIFIED)):
        invoke(comms, client, verdict)
        with pytest.raises(ToolRefusedError, match="no accepted exact-head source review"):
            promote(pair.context, carrier)
        assert not HeadReview.accepted(carrier.repo, carrier.head)
    invoke(comms, client, ReviewedHeadVerdict(ACCEPTED))
    assert HeadReview.accepted(client.repo, client.head)
    original, = inputs.source(pair.core.github).heads
    assert not HeadReview.accepted(original.repo, original.head)
    with pytest.raises(ToolRefusedError, match="no accepted exact-head source review"):
        promote(pair.context, carrier)
    invoke(comms, original, ReviewedHeadVerdict(ACCEPTED))
    promote(pair.context, carrier)
    assert HeadReview.accepted(carrier.repo, carrier.head)
    assert PromotedHeadReview.accepted(carrier.repo, carrier.head)
    assert StoredHeadReview.current_verdict(comms.goals.todos, carrier) is ReviewedHeadVerdict
    assert carrier.current().arm() == DraftMergeArm()
    assert run.accepted_heads() == ()
    invoke(comms, carrier, QualifiedHeadVerdict(QUALIFIED))
    assert not PromotedHeadReview.accepted(carrier.repo, carrier.head)
    invoke(comms, carrier, ReviewedHeadVerdict(ACCEPTED))
    assert HeadReview.accepted(carrier.repo, carrier.head)
    assert not PromotedHeadReview.accepted(carrier.repo, carrier.head)
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_a_moved_companion_refuses_final_review_without_writing_acceptance(
        mutual_sources: MutualSources, github_cli: GitHubFixture) -> None:
    pair = mutual_sources
    run = IntegrationRun(pair.checkout, pair.output, "a changed actual source invalidates the input")
    pair.capture(("P2", "P3"), run.risk)
    carrier = pair.publish_carrier(github_cli)
    pair.client.publish_change("Move the companion", *pair.client.write_files({
        Path("src/new_file.py"): "changed = True\n",
    }))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.client.github, DraftListedPullRequest(
        pair.client.number, "feature", pair.client.branch_commit, stamp, "main", reviewable_times=(stamp,),
    ))
    with pytest.raises(ToolRefusedError, match="moved"):
        promote(pair.context, carrier)
    assert not HeadReview.accepted(carrier.repo, carrier.head)
    assert not StoredHeadReview.head_reviews(pair.context.comms.goals.todos, carrier)


@pytest.mark.parametrize("damage", ("member", "tree", "carrier", "missing-source"))
def test_combined_promotion_refuses_changed_members_trees_carriers_and_missing_sources(
        mutual_sources: MutualSources, github_cli: GitHubFixture, damage: str) -> None:
    pair = mutual_sources
    comms = pair.context.comms
    inputs = pair.capture(("P2", "P3"), "every captured identity must survive promotion")
    carrier = pair.publish_carrier(github_cli)
    for source in inputs.sources:
        for head in source.heads:
            invoke(comms, head, ReviewedHeadVerdict(ACCEPTED))
    if damage == "member":
        moved = pair.core.publish_change("Change a captured local source", *pair.core.write_files({
            Path("src/agent_comms/moved.py"): "moved = True\n"}))
        opened(github_cli, pair.core, pair.core.number, moved)
    elif damage == "tree":
        path = pair.checkout.path / "not-reviewed.py"
        path.write_text("unreviewed = True\n")
        pair.core.git(pair.checkout.path, "add", "not-reviewed.py")
        pair.core.git(pair.checkout.path, "commit", "-m", f"Change the combined tree\n\n{inputs.trailer}")
        carrier = pair.publish_carrier(github_cli)
    elif damage == "carrier":
        # The same Git payload under another PR number is not its captured carrier.
        pair.core.git(pair.checkout.path, "push", "origin", f"{carrier.head}:refs/pull/102/head")
        carrier = opened(github_cli, pair.core, 102, carrier.head)
    else:
        # Keep valid canonical encoding while attempting to omit an actual declared client.
        incomplete = replace(inputs, sources=(inputs.source(pair.core.github),))
        pair.core.git(pair.checkout.path, "commit", "--allow-empty", "-m", incomplete.trailer)
        carrier = pair.publish_carrier(github_cli)
    with pytest.raises(ToolRefusedError):
        promote(pair.context, carrier)
    assert not PromotedHeadReview.accepted(carrier.repo, carrier.head)
    assert not StoredHeadReview.head_reviews(comms.goals.todos, carrier)
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


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
    posts = review_posts(github, request.head)
    assert [next(part for part in post if part.startswith("state=")) for post in posts] == [
        "state=pending", "state=success", "state=pending", "state=failure"]


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
    assert len(review_posts(github, request.head)) == 4


@pytest.mark.parametrize("ready,refused", ((False, False), (True, False), (True, True)))
def test_only_explicit_combined_promotion_arms_the_reviewed_head_and_reports_forge_failure(
        mutual_sources: MutualSources, github_cli: GitHubFixture, capsys,
        ready: bool, refused: bool) -> None:
    pair = mutual_sources
    comms = pair.context.comms
    inputs = pair.capture(("P2", "P3"), "explicit combined promotion owns enqueue")
    request = pair.publish_carrier(github_cli)
    for source in inputs.sources:
        for head in source.heads:
            invoke(comms, head, ReviewedHeadVerdict(ACCEPTED))
    assert request.pr not in {head.pr for head in inputs.source(request.repo).heads}
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())
    if ready:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        github_cli.put(request.repo, UnmergedListedPullRequest(
            request.pr, "combined", request.head, stamp, "main", reviewable_times=(stamp,)))
    if refused:
        github_cli.refuse_arm(request.repo, request.pr, "Merge permission denied")
    arguments = ["promote", "--repo", request.repo.to_wire(), "--pr", str(request.pr),
                 "--head", request.head, "--why", ACCEPTED.text]
    context = order.Commander.read_root(comms.root)
    if refused:
        with pytest.raises(ToolRefusedError, match="Merge permission denied"):
            judge.main(arguments, context)
    else:
        judge.main(arguments, context)
    arms = [call for call in github_cli.calls() if call[:2] == ("pr", "merge")]
    assert arms == ([("pr", "merge", str(request.pr), "--repo", request.repo.to_wire(),
                     "--auto", "--merge", "--match-head-commit", request.head)] if ready else [])
    if not ready:
        assert f"#{request.pr} is a draft, so not armed" in capsys.readouterr().out
    assert StoredHeadReview.current_verdict(comms.goals.todos, request) is ReviewedHeadVerdict
    assert PromotedHeadReview.accepted(request.repo, request.head)
    ReviewedHeadVerdict.require_merge(request, context.repos)
    # A stale pair of green carrier statuses cannot bypass current member decisions.
    member = inputs.source(pair.client.github).heads[0]
    QualifiedHeadVerdict(QUALIFIED).post(member.repo, member.head, "lead", "withdraw member")
    assert HeadReview.accepted(request.repo, request.head)
    assert MergeHeadReview.accepted(request.repo, request.head)
    with pytest.raises(ToolRefusedError, match="no accepted exact-head source review"):
        ReviewedHeadVerdict.require_merge(request, context.repos)


@pytest.mark.parametrize("verdict", (UnderReviewHeadVerdict(), RejectedHeadVerdict(REASON)))
def test_failed_permission_withdrawal_preserves_the_preceding_source_record(
    mutual_sources: MutualSources, github_cli: GitHubFixture, verdict: HeadVerdict,
) -> None:
    pair = mutual_sources
    comms = pair.context.comms
    inputs = pair.capture(("P2", "P3"), "withdraw before recording another source decision")
    request = pair.publish_carrier(github_cli)
    for source in inputs.sources:
        for head in source.heads:
            invoke(comms, head, ReviewedHeadVerdict(ACCEPTED))
    promote(pair.context, request)
    before = StoredHeadReview.head_reviews(comms.goals.todos, request)
    github_cli.refuse_status(request.repo, request.head, MergeHeadReview.context,
                             "Cannot withdraw the merge permission")
    with pytest.raises(ToolRefusedError, match="Cannot withdraw"):
        invoke(comms, request, verdict)
    assert StoredHeadReview.head_reviews(comms.goals.todos, request) == before
    assert HeadReview.accepted(request.repo, request.head)
    assert MergeHeadReview.accepted(request.repo, request.head)


@pytest.mark.parametrize("state", ("accepted", "unreviewed", "rejected", "stale", "uncontained", "status-only"))
def test_capture_derives_only_exact_saved_root_orderless_sources_in_the_actual_carrier(
    mutual_sources: MutualSources, github_cli: GitHubFixture, state: str,
) -> None:
    pair = mutual_sources
    comms = pair.context.comms
    peer = SourceRepository(checkout=pair.core.checkout, github=pair.core.github, number=9)
    peer.git(peer.checkout, "switch", "-c", "orderless-source", "main")
    commit = peer.publish_change("Actual orderless source", *peer.write_files({
        Path("src/agent_comms/orderless.py"): "orderless = 1\n"}))
    request = opened(github_cli, peer, peer.number, commit)
    if state not in ("unreviewed", "status-only"):
        invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    if state == "rejected":
        invoke(comms, request, RejectedHeadVerdict(REASON))
    if state == "status-only":
        ReviewedHeadVerdict(ACCEPTED).post(request.repo, request.head, "lead", "not a saved decision")
    if state == "stale":
        commit = peer.publish_change("A later unreviewed head", *peer.write_files({
            Path("src/agent_comms/orderless.py"): "orderless = 2\n"}))
        request = opened(github_cli, peer, peer.number, commit)
    primary = PullRequestHead(pair.core.github, pair.core.number, pair.core.branch_commit)
    invoke(comms, primary, ReviewedHeadVerdict(ACCEPTED))
    pair.prepare_carrier(("P2", "P3"))
    if state != "uncontained":
        pair.checkout.include(request)
    carrier = pair.publish_carrier(github_cli)
    inputs = IntegrationRun(pair.checkout, pair.output, "retain actual orderless source authority").qualify(
        pair.context, ("P2", "P3"), carrier=carrier)
    local = inputs.source(pair.core.github)
    expected = (primary, request) if state == "accepted" else (primary,)
    assert local.heads == expected  # The linked primary also has a NoHeadOrder review: no duplicate.
    comms.goals.todos.require_input_sources(inputs)
    assert all(link.number != request.pr for todo in comms.goals.todos.open_orders()
               for link in todo.order.checked().pull_requests)
    if state == "accepted":
        record, = StoredHeadReview.head_reviews(comms.goals.todos, request)
        assert record.order == NoHeadOrder() and record.actor == pair.context.incarnation
    else:
        altered = replace(local, heads=(primary, request))
        forged = replace(inputs, sources=tuple(altered if source.repository == pair.core.github else source
                                               for source in inputs.sources))
        if state == "uncontained":
            comms.goals.todos.require_input_sources(forged)
            with pytest.raises(ToolRefusedError, match="dropped a captured source parent"):
                altered.require_ancestry(pair.core, inputs.carrier.head)
        else:
            with pytest.raises(TodoError, match="saved root source authority"):
                comms.goals.todos.require_input_sources(forged)
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_failed_final_rejection_status_leaves_the_member_pending_not_old_green(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
) -> None:
    pair = mutual_sources
    comms = pair.context.comms
    inputs = pair.capture(("P2", "P3"), "a failed source publication must stay closed")
    request = pair.publish_carrier(github_cli)
    for source in inputs.sources:
        for head in source.heads:
            invoke(comms, head, ReviewedHeadVerdict(ACCEPTED))
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    PromotedHeadReview().post(request.repo, request.head, "lead", "explicit carrier promotion")
    MergeHeadReview.require_merge(request, pair.context.repos)
    member = inputs.source(pair.client.github).heads[0]
    github_cli.refuse_status(member.repo, member.head, HeadReview.context, "final source status refused", state="failure")
    with pytest.raises(ToolRefusedError, match="final source status refused"):
        invoke(comms, member, RejectedHeadVerdict(REASON))
    assert isinstance(StoredHeadReview.head_reviews(comms.goals.todos, member)[0].verdict, RejectedHeadVerdict)
    assert not HeadReview.accepted(member.repo, member.head)
    with pytest.raises(ToolRefusedError, match="no accepted exact-head source review"):
        MergeHeadReview.require_merge(request, pair.context.repos)


def test_source_acceptance_never_enqueues_and_promotion_refuses_an_individual_member(head_bus) -> None:
    comms, github, request = head_bus
    context = order.Commander.read_root(comms.root)
    arguments = ["--repo", request.repo.to_wire(), "--pr", str(request.pr),
                 "--head", request.head, "--why", ACCEPTED.text]
    judge.main(["ok", *arguments], context)
    assert HeadReview.accepted(request.repo, request.head)
    with pytest.raises(ToolRefusedError, match="captured combined carrier"):
        judge.main(["promote", *arguments], context)
    assert not PromotedHeadReview.accepted(request.repo, request.head)
    assert not any(call[:2] == ("pr", "merge") for call in github.calls())


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
    posts = review_posts(github, request.head)
    assert [next(part for part in post if part.startswith("state=")) for post in posts] == [
        "state=pending", "state=success", "state=pending"]


@pytest.mark.parametrize("verdict", (ReviewedHeadVerdict(ACCEPTED), QualifiedHeadVerdict(QUALIFIED)))
def test_moved_head_posts_and_records_nothing(head_bus, verdict):
    comms, github, request = head_bus
    moved = replace(request, head="a" * 40)
    with pytest.raises(ToolRefusedError):
        invoke(comms, moved, verdict)
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
    carried_posts = review_posts(github, merged)
    assert len(carried_posts) == 2 and "state=pending" in carried_posts[0]
    assert f"description=carried from {request.head[:9]}: same change" in carried_posts[1]

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


def test_refused_carry_records_nothing_and_alarms_only_its_head(head_bus, reviewed_source):
    """An archived repository answers 403 to the carried status: Core keeps no carry the
    forge lacks, the frame survives, and the refusal is that head's merge block."""
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
    github.refuse_status(request.repo, moved.head, HeadReview.context,
                         "gh: Repository was archived so is read-only. (HTTP 403)")

    pulls = ProgressRead()
    pulls.carry_reviews(comms.goals, request.repo)
    row = pulls.open(request.repo)[0]
    assert not row.has_head_review
    assert not StoredHeadReview.head_reviews(comms.goals.todos, moved)
    assert ("carried review status refused: gh: Repository was archived so is read-only. "
            "(HTTP 403)") in [block.what() for block in row.merge_blocks]


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
    held_orders = comms.goals.todos.board_held()
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
    assert not CommanderHistory.read(comms, frozenset()).action_at(subject).since(since)
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    assert owed() == []
    history = CommanderHistory.read(comms, frozenset())
    assert history.action_at(subject).since(since)
    assert subject.disposed(history, since)
    assert not subject.disposed(history, time.time() + 1)
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


def test_a_reviewing_mark_on_an_unclaimed_linked_order_is_recorded(head_bus):
    """Every order that takes ok or back on its linked head also takes the reviewing mark."""
    comms, _, request = head_bus
    todo = comms.goals.todos.get("P2")
    held = todo.held()
    RecordPullRequest(todo.revision, held.owner_identity,
                      PullRequestLink(request.repo, request.pr), held.generation).record(comms.goals, todo.id)
    todo = comms.goals.todos.get(todo.id)
    scope = OrderHeadReview.for_request(
        (todo,), request, lambda linked: UnquotedHeadReview.of(linked, ()))
    invoke(comms, request, UnderReviewHeadVerdict(), scope)
    changed = comms.goals.todos.get(todo.id)
    assert changed.order == todo.order
    assert changed.revision == todo.revision + 1
    review, = StoredHeadReview.head_reviews(comms.goals.todos, request)
    assert type(review.verdict) is UnderReviewHeadVerdict
    assert review.order.problem == todo.id


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
    posts = review_posts(github, head)
    assert len(posts) == 2 and "state=pending" in posts[0]
    assert any(f"(src +{grown})" in part for part in posts[1])

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
    posts = review_posts(github, head)
    assert len(posts) == 2 and "state=pending" in posts[0]
    assert any("(src +1)" in part for part in posts[1])


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
    assert found[9].endswith(" -> land or supersede it")
    assert found[7].endswith(" -> merge main into it")


@pytest.mark.parametrize(('verdict', 'cleared'), (
    (SupersededHeadVerdict(ACCEPTED, 'P2'), True),
    (AbandonedHeadVerdict(ACCEPTED), True),
))
def test_retained_head_verdict_precedes_the_published_lost_packet(
    head_bus, reviewed_source, installed_runtime, verdict, cleared,
):
    comms, github, request = head_bus
    source = reviewed_source
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    github.put(request.repo, ClosedListedPullRequest(
        request.pr, 'feature', request.head, closed_at=stamp, updated=stamp,
        base='main', reviewable_times=(stamp,)))
    asyncio.run(ClosedPullRequestWatch().run(lambda: nullcontext(comms)))
    frame = capture_frame(comms, 'lead', ProgressRead(()), CommanderHeartbeat())
    lost, = ClosedUnmergedPullRequest.scan_observed(comms, 'lead', (), frame)
    assert lost.request == request

    invoke(comms, request, verdict)
    reviewed, = StoredHeadReview.head_reviews(comms.goals.todos, request)
    published = time.time()
    assert reviewed.at.before(published)
    # A retained stale observation can be published after its exact-head verdict.
    # Publish the actual typed packet; no board rows or history records are rewritten.
    owner = comms.registry.require('lead').incarnation
    ping = replace(RulePing.capture(lost), since=RecordedEventTime(published))
    packet = CommanderDecisions(owner, (ping,), (ping,), ())
    CommanderUpdateStore.record(comms.root, packet)
    original, = packet.send(comms, ())
    previous = CommanderUpdate.latest(comms, owner)
    assert previous == (packet,)
    assert isinstance(original.task, CommanderDecisions)
    assert original.task == packet
    history = CommanderHistory.read(comms, frozenset(('lead',)))
    assert (CommanderUpdate.current_board(comms, 'lead') == ()) is cleared
    assert (packet.open_pings(history) == ()) is cleared
    if cleared:
        subject, = ping.subjects
        assert history.action_at(subject) == reviewed.at
        assert history.action_at(subject).before(published)

    for unrelated in (replace(request, head=source.main_commit),
                      replace(request, repo=RepositoryName('other', request.repo.name)),
                      replace(request, pr=request.pr + 1)):
        assert not UnlandedHeadSubject(unrelated).disposed(history, published)
    assert not HeadSubject(request).disposed(history, published)

    # Retained observations own age; actual publications own which lines were announced.
    delivery = CommanderUpdate.derive(owner, (), (lost,), previous,
                                      published=CommanderUpdate.published(comms, owner),
                                      now=time.time(), history=history)
    assert delivery.packet.handled[0].since == ping.since
    assert (delivery.open_pings == ()) is cleared
    CommanderUpdateStore.record(comms.root, delivery.packet)
    delivery.packet.send(comms, ())
    assert CommanderUpdate.latest(comms, owner) == (delivery.packet,)
    assert (CommanderUpdate.current_board(comms, 'lead') == ()) is cleared
    assert original.task.handled == (ping,)


def working_on(comms, source, tmp_path, name, branch):
    """A registered thread whose own worktree has ``branch`` checked out."""
    worktree = tmp_path / f"{name}-checkout"
    source.git(source.checkout, "worktree", "add", "-q", "-b", branch, str(worktree))
    comms.registry.declare(Thread(name, frozenset(), Checkout(worktree)), StoppedThreadStatus())


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

    snapshot = comms.registry.snapshot()
    assert snapshot.statuses["author"].stopped and not snapshot.statuses["author"].archived
    reader = WorkBranches(reviewed_source.checkout)
    worktrees = reader.worktrees()
    path = str(snapshot.require("author").worktree.path)
    assert reader.checkouts(snapshot, "fix/head-9", worktrees) == (
        ThreadBranchCheckout(("author",), path),)
    comms.registry.archive("author")
    archived = comms.registry.snapshot()
    assert archived.statuses["author"].archived
    # The same real Git worktree remains: only its registered holder changed.
    assert reader.checkouts(archived, "fix/head-9", worktrees) == (
        UnregisteredBranchCheckout(path),)
    pulls = ProgressRead(())
    rules = {rule.request.pr: rule for rule in OrderlessPullRequest.scan_orderless(comms.goals, pulls)}
    assert set(rules) == {9, 11}
    assert rules[9].holders == ()
    assert rules[9].line().startswith(
        f"UNORDERED {ready.repo.to_wire()}#9 {'e' * 9} (no order, and no registered thread has"
        " its branch checked out): required check debt-src failed")
    assert rules[9].line().endswith(" -> order its author or close it with a reason")
    row = next(row for row in pulls.open(ready.repo) if row.number == 9)
    assert pulls.working_on(comms.goals, ready.repo, row) == ()
    still_stuck, = StuckMerge.scan_pulls(comms.goals, pulls)
    assert still_stuck.request.pr == 12
    assert still_stuck.line().endswith(" -> fix debt-src at its owner at armer")


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


def test_only_a_discarding_verdict_may_decide_a_closed_head(head_bus):
    comms, github, request = head_bus
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    github.put(request.repo, ClosedListedPullRequest(
        request.pr, 'feature', request.head, closed_at=stamp, updated=stamp,
        base='main', reviewable_times=(stamp,)))
    with pytest.raises(Exception, match='no longer an open source'):
        invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    invoke(comms, request, AbandonedHeadVerdict(ACCEPTED))
    reviewed, = StoredHeadReview.head_reviews(comms.goals.todos, request)
    assert reviewed.verdict.supersedes()
    invoke(comms, request, SupersededHeadVerdict(ACCEPTED, 'P2'))
