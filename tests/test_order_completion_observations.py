"""Checked deliveries and existing PR links survive later source observations."""

from pathlib import Path

import pytest

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.runtime_resolution import RuntimeResolution
from source_revision_fixture import SourceRepository
from agent_comms.team.order import Team
from agent_comms.errors import TodoError
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.order_runs import ApplicationOrderRun
from agent_comms.planning.orders import (
    CorrectRunTodo, DeliveredProblemOrder, InstalledEvidence, ProgressRead,
    RecordPullRequest, TodoConflict,
)
from agent_comms.planning.pull_requests import PullRequestLink
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.team.order import main
from test_held_problem_orders import merge_two_commit_fix

pytest_plugins = ("test_held_problem_orders",)

WORKS = "The real application retains its checked delivery and installed evidence after every observation."


from agent_comms.planning.orders import UnmergedListedPullRequest
from github_fixture import GitHubFixture
from agent_comms.projects.work_branches import WorkBranches


@pytest.fixture
def application_team(held_team: Team) -> Team:
    ctx = held_team
    current = ctx.comms.goals.todos.get("P1")
    ctx.comms.goals.change_problem(
        current.id, CorrectRunTodo(
            expected_revision=current.revision, actor=ctx.incarnation,
            reason="The mechanical fixture needs an application run because this case keeps installed evidence",
            previous=current.order.run, run=ApplicationOrderRun(WORKS),
        ), ctx.incarnation,
    )
    return ctx


@pytest.mark.parametrize("installed", (False, True), ids=("delivered", "installed"))
@pytest.mark.parametrize("merge_style", ("--no-ff", "--squash"))
def test_checked_delivery_survives_later_merge_observations(
    application_team: Team, cited_source: SourceRepository, tmp_path: Path,
    installed: bool, merge_style: str,
) -> None:
    ctx = application_team
    store = ctx.comms.goals.todos
    current = store.get("P1")
    link = PullRequestLink(current.repo, 42)
    RecordPullRequest.for_order(current, current.creator_identity, link).record(
        ctx.comms.goals, current.id)
    _, head, merge = merge_two_commit_fix(cited_source, tmp_path / "bin", 42, merge_style)
    runtime = LaunchRuntime.installed()
    resolution = RuntimeResolution.at(runtime.prefix)
    resolution.model_copy(update={"core_commit": merge}).save(runtime.prefix)
    artifact = ctx.root / "completed-application.txt"
    artifact.write_text(WORKS)
    main(["done", "worker", "P1", "--seen", str(artifact)], ctx)
    delivered = store.get("P1")
    assert isinstance(delivered.order, DeliveredProblemOrder)
    accepted = delivered.order.delivery
    assert accepted.delivered == head
    if installed:
        main(["close", "P1", "--works", WORKS, "--artifact", str(artifact)], ctx)
    previous = store.get("P1")
    for observation in range(2):
        observed = previous.order.closed_by_merge(
            previous, ProgressRead(), ctx.comms.registry.storage_rule.checkout_base)
        store.record_merges(observed)
        current = store.get("P1")
        assert isinstance(current.order, DeliveredProblemOrder)
        assert current.order.delivery == accepted
        assert current.order.delivered(accepted) == current.order
        assert current.order.delivery.evidence.artifact() == str(artifact)
        assert current.order.installation_commits == (merge,)
        assert current.state is previous.state
        assert current.assignment == previous.assignment
        assert current.order.id == previous.order.id
        assert FieldCodec.decode(type(current), FieldCodec.encode(current)).order == current.order
        if installed:
            assert isinstance(current.order, InstalledEvidence)
            assert (current.order.works, current.order.artifact) == (WORKS, str(artifact))
        if observation:
            assert current.revision == previous.revision
        previous = current


def test_late_adoption_rejects_open_and_later_merged_work(
    application_team: Team, cited_source: SourceRepository, tmp_path: Path,
    github_cli: GitHubFixture,
) -> None:
    ctx = application_team
    store = ctx.comms.goals.todos
    _, head, merge = merge_two_commit_fix(cited_source, tmp_path / "bin", 42)
    current = store.get("P1")
    store.record_merges((current.order.merged(merge).waiting_in(current),))
    before = store.get("P1")
    worker = ctx.comms.registry.require("worker").incarnation
    link = PullRequestLink(before.repo, 43)
    github_cli.put(before.repo, UnmergedListedPullRequest(
        number=43, branch="fix/43", head=head, updated="2026-10-23T12:00:00Z",
        base=WorkBranches.main, reviewable_times=("2026-10-23T12:00:00Z",),
    ), body="The next repair remains open.")
    with pytest.raises(TodoError, match="already be merged into"):
        RecordPullRequest.for_order(before, worker, link).record(ctx.comms.goals, before.id)
    assert store.get(before.id) == before

    merge_two_commit_fix(cited_source, tmp_path / "bin", 43)
    with pytest.raises(TodoError, match="already be merged into"):
        RecordPullRequest.for_order(before, worker, link).record(ctx.comms.goals, before.id)
    assert store.get(before.id) == before


def test_holder_records_existing_merged_pr_without_reopening_work(
    application_team: Team, cited_source: SourceRepository, tmp_path: Path,
) -> None:
    ctx = application_team
    store = ctx.comms.goals.todos
    _, _, merge = merge_two_commit_fix(cited_source, tmp_path / "bin", 42)
    current = store.get("P1")
    store.record_merges((current.order.merged(merge).waiting_in(current),))
    before = store.get("P1")
    worker = ctx.comms.registry.require("worker").incarnation
    link = PullRequestLink(before.repo, 42)
    with pytest.raises(TodoConflict):
        RecordPullRequest(before.revision, worker, link, GoalId.mint()).record(
            ctx.comms.goals, before.id)
    assert store.get(before.id) == before
    foreign = PullRequestLink(RepositoryName("other", "unregistered"), 42)
    with pytest.raises(TodoError):
        RecordPullRequest.for_order(before, worker, foreign).record(ctx.comms.goals, before.id)
    assert store.get(before.id) == before
    RecordPullRequest.for_order(before, worker, link).record(ctx.comms.goals, before.id)
    after = store.get(before.id)
    assert after.order.pull_requests == (link,)
    assert type(after.order) is type(before.order)
    assert after.order.sha == merge
    assert (after.assignment, after.review_at, after.strike_count) == (
        before.assignment, before.review_at, before.strike_count)
    RecordPullRequest.for_order(after, worker, link).record(ctx.comms.goals, after.id)
    assert store.get(after.id) == after
    artifact = ctx.root / "late-linked-application.txt"
    artifact.write_text(WORKS)
    main(["done", "worker", "P1", "--seen", str(artifact)], ctx)
    completed = store.get(before.id).order
    assert isinstance(completed, DeliveredProblemOrder)
    assert isinstance(completed, type(before.order))
    assert completed.sha == before.order.sha == merge
    installed_merge = after.order.installed(WORKS, str(artifact))
    retained = installed_merge.delivered(completed.delivery)
    assert isinstance(retained, type(installed_merge))
    assert isinstance(retained, DeliveredProblemOrder)
    assert retained.sha == merge
    assert (retained.works, retained.artifact) == (WORKS, str(artifact))
