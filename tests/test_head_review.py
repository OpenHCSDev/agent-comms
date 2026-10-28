"""Head decisions through the native boundary, real stores, and the external gh process."""

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import chdir
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest


from agent_comms.commander import judge, order
from agent_comms.comms import Comms
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.build_identity import RepositoryCommit
from agent_comms.head_review import (
    DraftMergeArm, HeadReview, IntegrationInput, IntegrationSource, PullRequestHead, ReadPullRequestHead,
)
from agent_comms.integration import IntegrationRun
from agent_comms.heartbeat_subjects import CommanderHistory, HeadSubject
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import (
    HeadReviewTimerUp, IdlePullRequest, OrderlessPullRequest, OrderlessUnreviewedHead,
    OrderRefusingStandingRule, ReviewTimerUp, StuckMerge,
)
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.errors import TodoError, ToolRefusedError
from agent_comms.field_codec import FieldCodec, GoalId, MalformedRecord, RecordProblems
from agent_comms.tool_caller import ThreadCaller
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.work_branches import SourceGrowth, WorkBranches
from agent_comms.order_tools import CommsHeadVerdictTool
from agent_comms.todos import (
    CarriedHeadVerdict, ConflictMergeBlock, DraftListedPullRequest, FailedCheckMergeBlock, GrownHeadVerdict, GrowthReason, OrderHeadReview, LinkedHeadReview,
    NoHeadOrder, OrderClaim, ProgressRead, PullRequestLink, PullRequests, RecordPullRequest, RejectedHeadVerdict,
    HeadVerdict, QualifiedHeadVerdict, ReviewedHeadVerdict, StoredHeadReview, UnquotedHeadReview,
    StrikeReason, UnderReviewHeadVerdict, UnmergedListedPullRequest, VerdictReason, WithdrawTodo,
)
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
            record.verdict.require_merge(request)

    qualify_unchanged()  # Pending: the order still requires every unfinished clause.
    claim_order("P2", pull_request=request.pr, head=request.head)
    qualify_unchanged()  # Claimed: the delivery is neither accepted nor completed.
    todo = comms.goals.todos.get("P2")
    invoke(comms, request, UnderReviewHeadVerdict(), OrderHeadReview.for_request(
        (todo,), request, UnquotedHeadReview.of))
    qualify_unchanged()  # Reviewing: the recorded source cut changes no held work.
    posts = tuple(call for call in github.calls() if f"statuses/{request.head}" in call[1])
    assert len(posts) == 4
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
    inputs = IntegrationRun(pair.checkout, pair.output, "native qualified companion discovery").qualify(pair.context, ("P2",))
    assert inputs.source(pair.client.github).heads == (
        PullRequestHead(pair.client.github, pair.client.number, pair.client.branch_commit),)
    assert comms.goals.todos.get(before.id) == linked
    assert not HeadReview.accepted(primary.repo, primary.head)
    assert not any(call[:2] in (("pr", "create"), ("pr", "merge")) for call in github_cli.calls())


def test_companion_metadata_cannot_create_edit_or_adopt_missing_pr(
    linked_head_bus: tuple[Comms, GitHubFixture, PullRequestHead], companion_checkout: Path,
) -> None:
    comms, github, primary = linked_head_bus
    before = comms.goals.todos.get("P2")
    context = order.Commander.read_root(comms.root)
    with chdir(companion_checkout):
        for arguments in (
            ["--title", "Do not create a companion", "--body-file", "not-read.md"],
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
    assert len(arms) == 1 and HeadReview.accepted(request.repo, request.head)
    invoke(comms, request, QualifiedHeadVerdict(QUALIFIED))
    assert StoredHeadReview.current_verdict(comms.goals.todos, request) is QualifiedHeadVerdict
    assert not HeadReview.accepted(request.repo, request.head)
    assert tuple(call for call in github.calls() if call[:2] == ("pr", "merge")) == arms
    posts = tuple(call for call in github.calls() if f"statuses/{request.head}" in call[1])
    assert "state=pending" in posts[-1]
    # Only a later full acceptance can permit promotion and request an arm again.
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    assert HeadReview.accepted(request.repo, request.head)
    assert len(tuple(call for call in github.calls() if call[:2] == ("pr", "merge"))) == 2


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


def test_qualification_refuses_a_noncommander_before_any_record_or_public_status(linked_head_bus):
    comms, github, request = linked_head_bus
    before = comms.goals.todos.get("P2")
    tool = CommsHeadVerdictTool(
        request=request, operation=QualifiedHeadVerdict(QUALIFIED),
        order=OrderHeadReview.for_request((before,), request,
                                        lambda linked: LinkedHeadReview.of(linked, linked.order.edits[0])))
    with pytest.raises(ToolRefusedError):
        tool.invoke(comms, tool.arguments, caller=ThreadCaller("worker"))
    assert comms.goals.todos.get(before.id) == before
    assert not StoredHeadReview.head_reviews(comms.goals.todos, request)
    assert not any("statuses/" in call[1] for call in github.calls())


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
    assert request.change_identity(WorkBranches(source.checkout), "main") == moved.change_identity(
        WorkBranches(source.checkout), "main")
    pulls = ProgressRead()
    pulls.carry_reviews(comms.goals, request.repo)
    assert not StoredHeadReview.head_reviews(comms.goals.todos, moved)
    assert not pulls.open(request.repo)[0].has_head_review
    assert not any(f"statuses/{head}" in call[1] for call in github.calls())


@pytest.fixture
def captured_input(reviewed_source: SourceRepository) -> IntegrationInput:
    source = reviewed_source
    return IntegrationInput((IntegrationSource(
        RepositoryCommit(source.github, source.main_commit),
        (PullRequestHead(source.github, source.number, source.branch_commit),),
        source.git(source.checkout, "rev-parse", f"{source.branch_commit}^{{tree}}").strip(),
    ),))


def test_integration_metadata_is_canonical_and_ordinary_messages_have_no_digest(
        captured_input: IntegrationInput) -> None:
    inputs = captured_input
    assert FieldCodec.decode(IntegrationInput, FieldCodec.parse(inputs.canonical)) == inputs
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
        IntegrationInput((source, source))
    with pytest.raises(ToolRefusedError):
        captured_input.require_repositories((source.repository, RepositoryName.from_wire("other/repository")))


def test_companion_input_changes_the_identity_of_an_unchanged_local_patch(
        captured_input: IntegrationInput, reviewed_source: SourceRepository, tmp_path: Path,
        github_cli: GitHubFixture) -> None:
    source = reviewed_source
    companion = SourceRepository.create(tmp_path / "companion", RepositoryName.from_wire("example/client"))
    client = IntegrationSource(
        RepositoryCommit(companion.github, companion.main_commit),
        (PullRequestHead(companion.github, companion.number, companion.branch_commit),),
        companion.git(companion.checkout, "rev-parse", f"{companion.branch_commit}^{{tree}}").strip(),
    )
    inputs = IntegrationInput(tuple(sorted((*captured_input.sources, client), key=lambda row: row.repository.key)))
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
    inputs = run.qualify(pair.context, ("P2", "P3"))
    carrier = pair.publish_carrier(github_cli)
    invoke(comms, carrier, UnderReviewHeadVerdict())
    for verdict in (UnderReviewHeadVerdict(), QualifiedHeadVerdict(QUALIFIED)):
        invoke(comms, client, verdict)
        with pytest.raises(TodoError, match="no accepted exact-head source review"):
            invoke(comms, carrier, ReviewedHeadVerdict(ACCEPTED))
        assert not HeadReview.accepted(carrier.repo, carrier.head)
    invoke(comms, client, ReviewedHeadVerdict(ACCEPTED))
    assert HeadReview.accepted(client.repo, client.head)
    original, = inputs.source(pair.core.github).heads
    assert not HeadReview.accepted(original.repo, original.head)
    invoke(comms, carrier, ReviewedHeadVerdict(ACCEPTED))
    assert HeadReview.accepted(carrier.repo, carrier.head)
    assert StoredHeadReview.current_verdict(comms.goals.todos, carrier) is ReviewedHeadVerdict
    assert carrier.current().arm() == DraftMergeArm()
    assert run.accepted_heads() == ()
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_a_moved_companion_refuses_final_review_without_writing_acceptance(
        mutual_sources: MutualSources, github_cli: GitHubFixture) -> None:
    pair = mutual_sources
    run = IntegrationRun(pair.checkout, pair.output, "a changed actual source invalidates the input")
    run.qualify(pair.context, ("P2", "P3"))
    carrier = pair.publish_carrier(github_cli)
    pair.client.publish_change("Move the companion", *pair.client.write_files({
        Path("src/new_file.py"): "changed = True\n",
    }))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.client.github, DraftListedPullRequest(
        pair.client.number, "feature", pair.client.branch_commit, stamp, "main", reviewable_times=(stamp,),
    ))
    with pytest.raises(TodoError, match="moved"):
        invoke(pair.context.comms, carrier, ReviewedHeadVerdict(ACCEPTED))
    assert not HeadReview.accepted(carrier.repo, carrier.head)
    assert not StoredHeadReview.head_reviews(pair.context.comms.goals.todos, carrier)


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
    assert eight.change_identity(reader, "main") != four.change_identity(reader, "main")
    pulls = ProgressRead()
    pulls.carry_reviews(comms.goals, request.repo)
    assert not pulls.open(request.repo)[0].has_head_review
    assert not StoredHeadReview.head_reviews(comms.goals.todos, four)
    assert not any(f"statuses/{four.head}" in call[1] for call in github.calls())


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
    frame = HeartbeatFrame.capture(comms, "lead", ProgressRead(()))
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
