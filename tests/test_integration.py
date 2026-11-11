"""Integration policy at real TOML/Git/gh boundaries; no patched Core methods."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest
import tomli_w
from agent_comms_project import AgentCommsProject, ClientTypeErrors
from local_checker import LocalCheckerVerb
from module_layers import MaterializedClients, Package
from nominal_refactor_advisor import audit

from agent_comms.projects import integration
from agent_comms.errors import TodoError
from agent_comms.execution.test_lease import TestLease
from agent_comms.installer.runtime_resolution import PrivateUvProject, StackSources
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.goal_actions import CreateProblemGoalAction
from agent_comms.planning.order_words import VerdictReason
from agent_comms.planning.orders import (
    DraftListedPullRequest,
    MergedListedPullRequest,
    QualifiedHeadVerdict,
    ReviewedHeadVerdict,
    TodoConflict,
    UnmergedListedPullRequest,
)
from agent_comms.planning.pull_requests import PullRequestCommit
from agent_comms.projects.git_errors import (
    GitCommandError,
    UnavailableCommitError,
)
from agent_comms.projects.head_review import (
    BaseSide,
    CombinedInput,
    HeadReview,
    HeadSide,
    IntegrationInput,
    MergeHeadReview,
    PromotedHeadReview,
    PullRequestHead,
    ResolvedIntegrationSource,
)
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.order import Team
from agent_comms.transport.tool_caller import PeerCaller
from agent_comms.transport.tool_denial import StorageDenied
from agent_comms.transport.tools.orders import CommsTodoCreateTool
from agent_comms_hooks import ToolRefusedError
from fixture_git import ENVIRONMENT, SETTINGS
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources
from source_revision_fixture import SourceRepository

pytest_plugins = ("github_fixture", "test_holder_standing_rules", "integration_source_fixture")


def ci_event(root: Path, repository: RepositoryName, tested: str, event: str,
             document: Path) -> tuple[str, ...]:
    """The runner and event words the workflow spells on every CI source step."""
    return ("--runner-root", str(root), "--repository", repository.to_wire(),
            "--tested-commit", tested, "--event-name", event, "--event-path", str(document))


@pytest.fixture
def heads() -> tuple[PullRequestHead, ...]:
    repo = RepositoryName.from_wire("example/integration")
    return (PullRequestHead(repo, 7, "a" * 40), PullRequestHead(repo, 8, "b" * 40))


@pytest.mark.parametrize("problems", (("P2", "P3"), ("P3",)))
@pytest.mark.parametrize(("event", "moved_main_refuses"), (("pull_request", False), ("merge_group", True)))
def test_actual_ci_cli_uses_verified_carrier_not_synthetic_message_and_revalidates(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
    tmp_path: Path, event: str, moved_main_refuses: bool, problems: tuple[str, ...],
) -> None:
    pair = mutual_sources
    inputs = pair.capture(problems, "CI source graph")
    carrier = pair.publish_carrier(github_cli)
    local = inputs.source(pair.core.github)
    # Actual synthetic merge: its own message deliberately has no source metadata.
    tested = pair.core.git(pair.core.remote_path, "commit-tree", local.candidate_tree,
                           "-p", local.baseline.commit, "-p", carrier.head,
                           "-m", "Synthetic PR/queue merge; no input metadata").strip()
    pair.core.git(pair.core.remote_path, "update-ref", f"refs/pull/{carrier.pr}/merge", tested)
    assert not pair.core.has_commit(tested)
    checkout = integration.IntegrationCheckout.create(pair.core, tmp_path / "ci-checkout", tested)
    assert pair.core.git(checkout.path, "rev-parse", "HEAD^{tree}").strip() == local.candidate_tree
    assert pair.core.git(checkout.path, "show", "-s", "--format=%P", "HEAD").split() == [local.baseline.commit, carrier.head]
    assert IntegrationInput.from_message(Repo.git(checkout.path, "show", "--no-patch", "--format=%B", "HEAD")) == ()
    documents = {
        "pull_request": {"number": carrier.pr, "pull_request": {"head": {"sha": carrier.head}}},
        "merge_group": {"merge_group": {"head_sha": tested, "base_sha": local.baseline.commit,
                                        "head_ref": f"refs/heads/gh-readonly-queue/main/pr-{carrier.pr}-{carrier.head}"}},
    }
    document = tmp_path / "event.json"
    document.write_text(json.dumps(documents[event]))
    output = tmp_path / "resolved"
    environment = {**os.environ, **ENVIRONMENT,
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    words = ci_event(tmp_path, pair.core.github, tested, event, document)
    command = (sys.executable, "-m", "agent_comms.projects.integration")
    workflow_output = tmp_path / "workflow-output"
    result = subprocess.run((*command, "--resolve-ci-inputs", str(output),
                             "--workflow-output", str(workflow_output), *words), cwd=checkout.path,
                            env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert IntegrationInput.at(output) == inputs
    assert workflow_output.read_text() == (
        f"base={local.baseline.commit}\ncombined=true\ntested={inputs.tested}\n")
    assert inputs.carrier.pr == carrier.pr != pair.core.number
    assert inputs.pinned_sources(pair.core.github) == inputs.sources
    expected_local = tuple(
        PullRequestHead(pair.core.github, pair.core.number, pair.core.branch_commit)
        for problem in problems if problem == "P2"
    )
    assert local.heads == expected_local
    assert local.has_changes(pair.core) == ("P2" in problems)
    assert inputs.source(pair.client.github).has_changes(pair.client)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD^").strip() == inputs.carrier.head
    for source in inputs.sources:
        for side in (BaseSide, HeadSide):
            side.require_materialized(output, source)
    original = pair.core.git(pair.core.checkout, "rev-parse", "HEAD").strip()
    result = subprocess.run((*command, "--revalidate-ci-inputs", str(output), *words),
                            cwd=checkout.path, env=environment, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    pair.client.git(pair.client.checkout, "push", "origin", "HEAD:main")
    stale = subprocess.run((*command, "--revalidate-ci-inputs", str(output), *words),
                           cwd=checkout.path,
                           env=environment, capture_output=True, text=True, timeout=120)
    # A pull request keeps the bases it captured; only the merge group must be current.
    assert (stale.returncode != 0 and "main moved" in stale.stderr) == moved_main_refuses, stale.stderr
    assert pair.core.git(pair.core.checkout, "rev-parse", "HEAD").strip() == original
    assert not any(call[:2] == ("pr", "merge") or "state=success" in call for call in github_cli.calls())


def test_a_stacked_merge_group_is_current_while_main_lies_under_its_base(tmp_path: Path) -> None:
    """With more than one queue entry building, a group's base is the queue commit of the group
    ahead of it, which GitHub lands on main first: runs 1694, 1695 and 1699 failed "main moved;
    recapture" on exactly that. Main under the base is current; a main that moved sideways, past
    the queue, is not."""
    source = SourceRepository.create(tmp_path / "source")
    source.git(source.checkout, "switch", "-c", "queue-ahead")
    ahead = source.commit_change("The group ahead", *source.write_files({Path("ahead.py"): "x = 1\n"}))
    group = integration.MergeGroupSources(
        source, ahead, PullRequestHead(source.github, source.number, source.branch_commit),
        runner=integration.CiRunner(root=tmp_path))
    assert group.main_is_current(source, ahead)
    # Exact equality, which every other repository's main and every other event still owes.
    assert not integration.TestedSources.main_is_current(group, source, ahead)
    source.git(source.checkout, "switch", "main")
    source.commit_change("A hotfix past the queue", *source.write_files({Path("hotfix.py"): "y = 1\n"}))
    source.git(source.checkout, "push", "origin", "main")
    assert not group.main_is_current(source, ahead)


def test_actual_ordinary_ci_cli_captures_main_once_and_never_steals_an_unlinked_overlay(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    head = pair.core.branch_commit
    baseline = pair.core.main_commit
    tree = pair.core.git(pair.core.checkout, "rev-parse", f"{head}^{{tree}}").strip()
    tested = pair.core.git(pair.core.remote_path, "commit-tree", tree, "-p", baseline,
                           "-p", head, "-m", "Actual ordinary synthetic merge").strip()
    pair.core.git(pair.core.remote_path, "update-ref", f"refs/pull/{pair.core.number}/merge", tested)
    assert not pair.core.has_commit(tested)
    checkout = integration.IntegrationCheckout.create(pair.core, tmp_path / "ordinary-ci", tested)
    assert pair.core.git(checkout.path, "rev-parse", "HEAD^{tree}").strip() == tree
    assert pair.core.git(checkout.path, "show", "-s", "--format=%P", "HEAD").split() == [baseline, head]
    # The local command's request is the one CI's workflow spells, and CI's code resolves it.
    request = LocalCheckerVerb.parse(["pr", str(pair.core.number)]).request(
        tmp_path, pair.core.github, tested)
    parser = argparse.ArgumentParser()
    integration.CiEvent.add_arguments(parser)
    assert integration.CiEvent.from_arguments(parser.parse_args(ci_event(
        tmp_path, pair.core.github, tested, "pull_request", request.document))) == request
    assert json.loads(request.document.read_text())["pull_request"]["head"]["sha"] == head
    words = integration.CiEvent.to_arguments(request)
    output = tmp_path / "ordinary-inputs"
    environment = {**os.environ, **ENVIRONMENT,
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    workflow_output = tmp_path / "ordinary-workflow-output"
    result = subprocess.run((sys.executable, "-m", "agent_comms.projects.integration", "--resolve-ci-inputs", str(output),
                             "--workflow-output", str(workflow_output), *words),
                            cwd=checkout.path, env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    inputs = IntegrationInput.at(output)
    assert workflow_output.read_text() == f"base={baseline}\ncombined=false\ntested={inputs.tested}\n"
    core = inputs.source(pair.core.github)
    client = inputs.source(pair.client.github)
    assert core.baseline.commit == baseline
    assert core.heads == (PullRequestHead(pair.core.github, pair.core.number, head),)
    assert client.heads == () and client.baseline.commit == pair.client.main_commit
    assert inputs.pinned_sources(pair.core.github) == (core,)
    resource = materialized_clients(output, inputs)
    base = BaseSide.require_materialized(output, core)
    candidate = HeadSide.require_materialized(output, core)
    assert Package(base, resource.at(checkout.path, baseline, tested, BaseSide)).missing_names == ()
    assert {name for _, name in Package(candidate, resource.at(checkout.path, baseline, tested, HeadSide)).missing_names} == {"old_api"}
    assert not any("state=success" in call or call[:2] == ("pr", "merge") for call in github_cli.calls())
    ordinary = subprocess.run((sys.executable, "-m", "agent_comms.projects.integration", "--check-merge-inputs",
                               str(tmp_path / "ordinary"), *words), cwd=checkout.path,
                              env=environment, capture_output=True, text=True, timeout=120)
    assert ordinary.returncode == 0 and "combined=False" in ordinary.stdout, ordinary.stderr
    request.document.write_text(json.dumps({"merge_group": {
        "head_sha": tested, "base_sha": baseline,
        "head_ref": f"refs/heads/gh-readonly-queue/main/pr-{pair.core.number}-{head}"}}))
    queued = subprocess.run((sys.executable, "-m", "agent_comms.projects.integration", "--check-merge-inputs",
                             str(tmp_path / "ordinary-queue"),
                             *integration.CiEvent.to_arguments(
                                 replace(request, event=integration.MergeGroupSources))),
                            cwd=checkout.path, env=environment,
                            capture_output=True, text=True, timeout=120)
    assert queued.returncode == 0 and "combined=False" in queued.stdout, queued.stderr
    assert not HeadReview.accepted(pair.core.github, tested)
    assert not MergeHeadReview.accepted(pair.core.github, tested)


def test_integration_checkout_refuses_commits_absent_from_its_own_origin(
    source_repository: SourceRepository, tmp_path: Path,
) -> None:
    source = source_repository
    foreign = SourceRepository.create(tmp_path / "foreign-origin")
    # publish_change publishes the PR branch's descendant, so it commits on feature.
    foreign.git(foreign.checkout, "switch", "feature")
    foreign_head = foreign.publish_change("Foreign-only source", *foreign.write_files({
        Path("foreign.py"): "foreign = True\n",
    }))
    before = source.git(source.checkout, "rev-parse", "HEAD", "HEAD^{tree}")
    published = source.git(source.remote_path, "show-ref")
    for index, commit in enumerate((foreign_head, "f" * 40)):
        destination = tmp_path / f"unavailable-{index}"
        assert not source.has_commit(commit)
        with pytest.raises(UnavailableCommitError):
            integration.IntegrationCheckout.create(source, destination, commit)
        assert not destination.exists()
        assert source.git(source.checkout, "rev-parse", "HEAD", "HEAD^{tree}") == before
        assert source.git(source.remote_path, "show-ref") == published


@pytest.mark.parametrize("event", ("pull_request", "merge_group"))
def test_required_hosted_merge_gate_distinguishes_source_review_from_combined_promotion(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path, event: str,
) -> None:
    pair = mutual_sources
    inputs = pair.capture(("P2", "P3"), "hosted gate must refuse an individually armed source")
    carrier = pair.publish_carrier(github_cli)
    for source in inputs.sources:
        for head in source.heads:
            ReviewedHeadVerdict(VerdictReason("The exact source is accepted for this actual combined carrier")).post(
                head.repo, head.head, "colleague", "source")
    # A source review on the combined head is still not a merge promotion.
    ReviewedHeadVerdict(VerdictReason("The combined source is reviewed but not yet promoted for merge")).post(
        carrier.repo, carrier.head, "colleague", "combined source")
    local = inputs.source(pair.core.github)
    tested = pair.core.git(pair.core.checkout, "commit-tree", local.candidate_tree,
                           "-p", local.baseline.commit, "-p", carrier.head,
                           "-m", "Actual queue/PR merge").strip()
    checkout = integration.IntegrationCheckout.create(pair.core, tmp_path / "merge-gate", tested)
    documents = {
        "pull_request": {"number": carrier.pr, "pull_request": {"head": {"sha": carrier.head}}},
        "merge_group": {"merge_group": {"head_sha": tested, "base_sha": local.baseline.commit,
                        "head_ref": f"refs/heads/gh-readonly-queue/main/pr-{carrier.pr}-{carrier.head}"}},
    }
    document = tmp_path / "event.json"
    document.write_text(json.dumps(documents[event]))
    environment = {**os.environ, **ENVIRONMENT,
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    command = (sys.executable, "-m", "agent_comms.projects.integration",
               *ci_event(tmp_path, pair.core.github, tested, event, document), "--check-merge-inputs")
    result = subprocess.run((*command, str(tmp_path / "before-promotion")), cwd=checkout.path,
                            env=environment, capture_output=True, text=True, timeout=120)
    assert (result.returncode == 0) == (event == "pull_request"), result.stdout + result.stderr
    if event == "merge_group":
        assert "no explicit reviewed merge promotion" in result.stderr
    ReviewedHeadVerdict(VerdictReason("The actual combined carrier is explicitly reviewed for merge")).post(
        carrier.repo, carrier.head, "colleague", "source")
    PromotedHeadReview().post(carrier.repo, carrier.head, "colleague", "promotion")
    promoted = subprocess.run((*command, str(tmp_path / "after-promotion")), cwd=checkout.path,
                              env=environment, capture_output=True, text=True, timeout=120)
    assert promoted.returncode == 0, promoted.stdout + promoted.stderr
    assert HeadReview.accepted(carrier.repo, tested) is (event == "merge_group")
    assert MergeHeadReview.accepted(carrier.repo, tested) is (event == "merge_group")
    member = inputs.source(pair.client.github).heads[0]
    QualifiedHeadVerdict(VerdictReason("This member is no longer accepted for the combined source")).post(
        member.repo, member.head, "colleague", "withdraw member")
    stale = subprocess.run((*command, str(tmp_path / "after-member-withdrawal")), cwd=checkout.path,
                           env=environment, capture_output=True, text=True, timeout=120)
    assert stale.returncode != 0 and "no accepted exact-head source review" in stale.stderr
    assert not MergeHeadReview.accepted(carrier.repo, tested)
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_resolved_companion_keeps_multiple_original_links_and_the_published_carrier_tree(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
) -> None:
    pair = mutual_sources
    peer = SourceRepository(checkout=pair.client.checkout, github=pair.client.github, number=8)
    peer.git(peer.checkout, "switch", "-c", "second-client-source", "main")
    peer_head = peer.publish_change("Independent companion source", *peer.write_files({
        Path("src/extra.py"): "extra = 1\n"}))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(peer.github, DraftListedPullRequest(
        peer.number, "second-client-source", peer_head, stamp, "main", reviewable_times=(stamp,)))
    comms = pair.context.comms
    pair.link_companion("P4", peer, "Include the second existing companion source")
    member_heads = (PullRequestHead(pair.client.github, pair.client.number, pair.client.branch_commit),
                    PullRequestHead(peer.github, peer.number, peer_head))
    resolved = integration.IntegrationCheckout.create(pair.client, tmp_path / "resolved-client", pair.client.main_commit)
    resolved.combine_sources(pair.client.main_commit, member_heads)
    # The reviewed carrier may contain a resolved change; replaying member merges loses it.
    (resolved.path / "src/resolution.py").write_text("resolved = True\n")
    peer.git(resolved.path, "add", "src/resolution.py")
    peer.git(resolved.path, "commit", "-m", "Resolve the actual companion carrier")
    companion_head = peer.git(resolved.path, "rev-parse", "HEAD").strip()
    number = 102
    peer.git(resolved.path, "push", "origin", f"{companion_head}:refs/pull/{number}/head")
    github_cli.put(peer.github, DraftListedPullRequest(
        number, "resolved-companion", companion_head, stamp, "main", reviewable_times=(stamp,)))
    companion = PullRequestHead(peer.github, number, companion_head)
    inputs = pair.capture(("P2", "P3", "P4"), "preserve actual resolved companion sources", companions=(companion,))
    carrier = pair.publish_carrier(github_cli)
    source = inputs.source(peer.github)
    assert isinstance(source, ResolvedIntegrationSource)
    assert source.carrier == companion and source.heads == member_heads
    assert inputs == CombinedInput.at(pair.output)
    assert peer.git(HeadSide.checkout(pair.output, peer.github), "rev-parse", "HEAD").strip() == companion_head
    inputs.validate(pair.context.repos, carrier)
    replaced = replace(source, candidate_tree=source.baseline.commit)
    with pytest.raises(ToolRefusedError, match="tree differs"):
        replaced.require_tree(peer)
    github_cli.put(peer.github, DraftListedPullRequest(
        number, "resolved-companion", peer_head, stamp, "main", reviewable_times=(stamp,)))
    with pytest.raises(ToolRefusedError, match="moved"):
        inputs.validate(pair.context.repos, carrier)
    assert comms.goals.todos.get("P2").repo == pair.core.github
    assert {link.number for pid in ("P3", "P4") for link in comms.goals.todos.get(pid).order.checked().pull_requests} == {pair.client.number, peer.number}
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_actual_main_cli_keeps_merged_local_members_exact_and_refuses_later_head_changes(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
) -> None:
    pair = mutual_sources
    peer = SourceRepository(checkout=pair.core.checkout, github=pair.core.github, number=8)
    peer.git(peer.checkout, "switch", "-c", "second-local-source", "main")
    peer_head = peer.publish_change("Independent local source", *peer.write_files({
        Path("src/agent_comms/extra.py"): "extra = 1\n"}))
    peer.git(peer.checkout, "switch", "feature")
    assert peer.git(peer.checkout, "ls-remote", "origin", f"refs/pull/{peer.number}/head").split() == [
        peer_head, f"refs/pull/{peer.number}/head"]
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(peer.github, DraftListedPullRequest(
        peer.number, "second-local-source", peer_head, stamp, "main", reviewable_times=(stamp,)))
    pair.link_companion("P4", peer, "Include the existing local source")
    inputs = pair.capture(("P2", "P3", "P4"), "actual main tuple")
    carrier = pair.publish_carrier(github_cli)
    pair.core.git(pair.core.checkout, "switch", "main")
    pair.core.git(pair.core.checkout, "merge", "--no-ff", "--no-edit", carrier.head)
    tested = pair.core.git(pair.core.checkout, "rev-parse", "HEAD").strip()
    pair.core.git(pair.core.checkout, "push", "origin", "main")
    for number, head in ((carrier.pr, carrier.head), (peer.number, peer_head)):
        github_cli.put(pair.core.github, MergedListedPullRequest(
            number, "feature", head, stamp, "main", reviewable_times=(stamp,),
            merge=PullRequestCommit(tested), merged=stamp))
    document = tmp_path / "main-event.json"
    document.write_text(json.dumps({"ref": "refs/heads/main", "after": tested}))
    output = tmp_path / "main-inputs"
    environment = {**os.environ, **ENVIRONMENT,
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    command = (sys.executable, "-m", "agent_comms.projects.integration",
               *ci_event(tmp_path, pair.core.github, tested, "push", document))
    result = subprocess.run((*command, "--resolve-ci-inputs", str(output)), cwd=pair.core.checkout,
                            env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert IntegrationInput.at(output) == inputs
    result = subprocess.run((*command, "--revalidate-ci-inputs", str(output)), cwd=pair.core.checkout,
                            env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    github_cli.put(peer.github, MergedListedPullRequest(
        peer.number, "second-local-source", "f" * 40, stamp, "main", reviewable_times=(stamp,),
        merge=PullRequestCommit(tested), merged=stamp))
    stale = subprocess.run((*command, "--revalidate-ci-inputs", str(output)), cwd=pair.core.checkout,
                           env=environment, capture_output=True, text=True, timeout=120)
    assert stale.returncode != 0 and "exact captured source" in stale.stderr
    assert not any("state=success" in call or call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_real_client_checker_compares_the_matched_tuple_in_its_one_interpreter(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    plugin = ('from mypy.plugin import Plugin\nfrom agent_comms.api import old_api\n'
              'from performance.capture_live import capture\n'
              'def plugin(version: str) -> type[Plugin]:\n'
              '    assert old_api() == capture() == "baseline"\n    return Plugin\n')
    capture = 'from agent_comms.api import old_api\n\ndef capture() -> str:\n    return old_api()\n'
    script = ('from agent_comms.api import old_api\nfrom performance.capture_live import capture\n'
              'assert old_api() == capture() == "baseline"\n')
    # The native config and its plugin are real baseline source, then migrated on feature.
    pair.client.git(pair.client.checkout, "switch", "main")
    project = tomllib.loads((pair.client.checkout / "pyproject.toml").read_text())
    project["tool"]["mypy"] = {"plugins": ["mypy_source_plugin.py"], "mypy_path": ["src", "tools"]}
    pair.client.commit_change("Declare baseline script roots and native mypy plugin", *pair.client.write_files({
        Path("pyproject.toml"): tomli_w.dumps(project), Path("mypy_source_plugin.py"): plugin,
        Path("tools/performance/capture_live.py"): capture, Path("tools/acceptance.py"): script}))
    pair.client.git(pair.client.checkout, "push", "origin", "main")
    pair.client.git(pair.client.checkout, "switch", "feature")
    pair.client.git(pair.client.checkout, "merge", "--no-edit", "main")
    head = pair.client.publish_change("Migrate scripts and native plugin alongside the client", *pair.client.write_files({
        path: text.replace("old_api", "new_api").replace("baseline", "candidate")
        for path, text in ((Path("mypy_source_plugin.py"), plugin),
                           (Path("tools/performance/capture_live.py"), capture),
                           (Path("tools/acceptance.py"), script))}))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.client.github, DraftListedPullRequest(
        pair.client.number, "feature", head, stamp, "main", reviewable_times=(stamp,)))
    inputs = pair.capture(("P2", "P3"), "real client checker tuple and native plugins")
    source = inputs.source(pair.core.github)
    head = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    clients = materialized_clients(pair.output, inputs)
    project = AgentCommsProject(pair.core.checkout, clients=clients)
    assert len(ClientTypeErrors.parts(project, source.baseline.commit, head,
                                     {"src/agent_comms/api.py"}, "src/")) == 2
    for side in (BaseSide, HeadSide):
        client, = clients.at(pair.core.checkout, source.baseline.commit, head, side)
        assert client.import_paths == tuple(str(client.checkout / path) for path in ("src", "tools"))
        assert client.files_importing("agent_comms") == [
            "mypy_source_plugin.py", "src/workflow.py", "tools/acceptance.py", "tools/performance/capture_live.py"]
        # Python's script entrypoint and the plugin's import must see this same cut.
        entry = subprocess.run((sys.executable, "tools/acceptance.py"), cwd=client.checkout,
            # The probe imports from the materialized client; its bytecode would dirty that
            # source before the next side requires it clean.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join((
                str(side.require_materialized(pair.output, source) / "src"), *client.import_paths))},
            capture_output=True, text=True, timeout=30)
        assert entry.returncode == 0, entry.stdout + entry.stderr
    # Both namespace files are selected under --explicit-package-bases: a duplicate
    # performance/tools.performance identity aborts the real checker, not a zero count.
    result = audit.gate.compare(project, source.baseline.commit, head, "src", 1,
                                   (ClientTypeErrors,))
    assert not result.failed
    assert all(value == 0 for value in result.delta.values())


def test_ci_event_refuses_unrelated_graph_and_carrier_without_materialization(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
) -> None:
    pair = mutual_sources
    carrier = PullRequestHead(pair.core.github, pair.core.number, pair.core.branch_commit)
    # main is not a two-parent PR test graph, regardless of plausible event text.
    with pytest.raises(integration.IntegrationFailed, match="two-parent"):
        integration.PullRequestSources.resolve(pair.core, pair.core.main_commit,
            {"number": carrier.pr, "pull_request": {"head": {"sha": carrier.head}}},
            runner=integration.CiRunner(root=tmp_path))
    with pytest.raises(integration.IntegrationFailed, match="queued carrier"):
        integration.MergeGroupSources.resolve(pair.core, pair.core.main_commit,
            {"merge_group": {"head_sha": pair.core.main_commit, "base_sha": pair.core.main_commit,
                             "head_ref": "refs/heads/not-a-GitHub-queue-reference"}},
            runner=integration.CiRunner(root=tmp_path))
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_stack_source_identity_normalizes_package_spelling_and_refuses_duplicate_aliases() -> None:
    source = '{git = "https://github.com/example/client.git", branch = "main"}'
    project = '[project]\ndependencies=[]\n[tool.uv.sources]\n'
    assert StackSources.declared_sources(PrivateUvProject.decode(
        project + f'Source_Client = {source}\n'
    )) == {"source-client": ("https://github.com/example/client.git", "main")}
    with pytest.raises(ValueError, match="declares package source-client twice"):
        StackSources.declared_sources(PrivateUvProject.decode(
            project + f'Source_Client = {source}\nsource-client = {source}\n'
        ))


def test_selection_uses_the_project_declaration_and_unions_existing_files(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["checks"]\n'
        'python_files = ["spec_*.py"]\n'
    )
    selected = Path("checks/spec_owner.py")
    ignored = Path("tests/test_copy.py")
    for path in (selected, ignored):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("def test_ok(): pass\n")
    selection = integration.TestSelection.read(tmp_path)
    assert selection.files(tmp_path, (selected, ignored, selected,
                                      Path("checks/spec_missing.py"))) == (str(selected),)


@pytest.mark.parametrize("configuration", (
    'testpaths = ["checks"]\n',
    '',
))
def test_selection_uses_pytest_defaults_without_loading_project_code(
    tmp_path: Path, configuration: str,
):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\n' + configuration
    )
    (tmp_path / "conftest.py").write_text(
        'raise RuntimeError("selection must not execute project code")\n'
    )
    selected = Path("checks/test_owner.py")
    ignored = Path("checks/owner.py")
    for path in (selected, ignored):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("def test_ok(): pass\n")
    assert integration.TestSelection.read(tmp_path).files(
        tmp_path, (selected, ignored, selected),
    ) == (str(selected),)


def test_only_accepted_ready_members_are_selected(github_cli, tmp_path: Path):
    source = SourceRepository.create(tmp_path / "integration-source")
    github_cli.configure(source.github)
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(source.github, UnmergedListedPullRequest(
        7, "ready", source.branch_commit, stamp, "main", reviewable_times=(stamp,),
    ))
    github_cli.put(source.github, DraftListedPullRequest(
        8, "draft", source.branch_commit, stamp, "main", reviewable_times=(stamp,),
    ))
    (source.checkout / "stage.py").write_text("source_stage = True\n")
    source.git(source.checkout, "add", "stage.py")
    source.git(source.checkout, "commit", "-m", "Prepare an incomplete source stage")
    qualified = source.git(source.checkout, "rev-parse", "HEAD").strip()
    source.git(source.checkout, "push", "origin", "HEAD:refs/pull/9/head")
    github_cli.put(source.github, UnmergedListedPullRequest(
        9, "qualified", qualified, stamp, "main", reviewable_times=(stamp,),
    ))
    github_cli.put(source.github, UnmergedListedPullRequest(
        10, "unaccepted", source.main_commit, stamp, "main", reviewable_times=(stamp,),
    ))
    QualifiedHeadVerdict(VerdictReason("The source stage is reviewed while paired implementation remains incomplete")).post(
        source.github, qualified, "colleague", "source stage",
    )
    run = integration.IntegrationRun(integration.IntegrationCheckout(source.checkout),
                                     tmp_path / "output", "ready-head selection")
    assert run.accepted_heads() == ()
    ReviewedHeadVerdict(VerdictReason("The declared class owns the reviewed behavior")).post(
        source.github, source.branch_commit, "colleague", "integration",
    )
    assert run.accepted_heads() == (PullRequestHead(source.github, 7, source.branch_commit),)


def test_qualification_captures_actual_orders_without_reviewing_or_arming_sources(
        mutual_sources: MutualSources, github_cli: GitHubFixture) -> None:
    pair = mutual_sources
    run = integration.IntegrationRun(pair.checkout, pair.output, "mutually migrated API/import source")
    inputs = pair.capture(("P2", "P3"), run.risk)
    assert FieldCodec.decode(IntegrationInput, FieldCodec.parse((pair.output / "input.json").read_bytes())) == inputs
    carrier = Repo.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    assert IntegrationInput.from_message(Repo.git(pair.checkout.path, "show", "--format=%B", "--no-patch", carrier)) == (inputs,)
    for source in inputs.sources:
        assert len(source.heads) == 1
        assert Repo.descends(HeadSide.checkout(pair.output, source.repository), source.heads[0].head, "HEAD")
        assert Repo.git(BaseSide.checkout(pair.output, source.repository), "rev-parse", "HEAD").strip() == source.baseline.commit
        assert Repo.git(HeadSide.checkout(pair.output, source.repository), "rev-parse", "HEAD^{tree}").strip() == source.candidate_tree
        assert not HeadReview.accepted(source.repository, source.heads[0].head)
    assert run.accepted_heads() == ()
    assert inputs.carrier.pr != pair.core.number
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD^").strip() == inputs.carrier.head
    assert all(not head.same_pull_request(inputs.carrier) for source in inputs.sources for head in source.heads)
    assert not any(call[:2] == ("pr", "merge") or "POST" in call for call in github_cli.calls())


def test_first_publication_requires_the_actual_holder_not_creator_authority(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("", checkout_owner="colleague")
    pair = replace(pair, context=pair.context.model_copy(update={"actor": PeerCaller("colleague")}))
    primary = pair.context.comms.goals.todos.get("P3")
    primary.require_actor(pair.context.actor_identity, primary.held().generation)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    with pytest.raises(TodoConflict):
        pair.publish(pair.output)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert not pair.output.exists()
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 0


def test_publication_refuses_sibling_checkout_before_commit_push_or_creation(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("", checkout_owner="colleague")
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    refs = pair.core.git(pair.core.remote_path, "show-ref")
    with pytest.raises(StorageDenied):
        pair.publish(pair.output)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert pair.core.git(pair.core.remote_path, "show-ref") == refs
    assert pair.core.git(pair.checkout.path, "status", "--porcelain") == ""
    assert not pair.output.exists()
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 0


def test_existing_carrier_capture_refuses_sibling_checkout_before_mutation(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("", checkout_owner="colleague")
    carrier = pair.publish_carrier(pair.github)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    refs = pair.core.git(pair.core.remote_path, "show-ref")
    with pytest.raises(StorageDenied):
        integration.IntegrationRun(pair.checkout, pair.output, "Refuse foreign capture").qualify(
            pair.context, ("P3",), carrier=carrier)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert pair.core.git(pair.core.remote_path, "show-ref") == refs
    assert not pair.output.exists()


@pytest.mark.parametrize("reply_refusal", ("", "Connection lost after actual PR creation"))
def test_first_publication_recovers_actual_carrier_without_repeating_creation(
    mutual_sources: MutualSources, reply_refusal: str,
) -> None:
    pair = mutual_sources.starting_publication(reply_refusal)
    with pytest.raises(TodoError) if reply_refusal else nullcontext():
        pair.publish(pair.output)
    inputs = pair.publish(pair.output.with_name("resumed-publication"))
    commit = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    current = PullRequestHead.at(pair.core.github, pair.carrier_number)
    assert current.head == commit
    inputs.validate(pair.context.repos, current)
    linked = pair.context.comms.goals.todos.get("P3").order.checked().pull_requests
    assert inputs.carrier.repo == pair.core.github
    assert inputs.carrier.pr == pair.carrier_number
    assert any(link.repo == current.repo and link.number == current.pr for link in linked)
    assert pair.publish(pair.output.with_name("verified-publication")) == inputs
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == commit
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1
    assert FieldCodec.decode(IntegrationInput, FieldCodec.parse(
        (pair.output.with_name("verified-publication") / "input.json").read_bytes())) == inputs


def test_publication_resumes_captured_commit_after_real_push_refusal(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("Reply lost after creation")
    with pytest.raises(TodoError):
        pair.publish(pair.output)
    refusal = shutil.which("false")
    assert refusal, "The foreign Git refusal fixture requires the false executable"
    hook = pair.core.remote_path / "hooks" / "pre-receive"
    hook.symlink_to(refusal)
    with pytest.raises(GitCommandError):
        pair.publish(pair.output.with_name("refused-final-push"))
    captured = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    hook.unlink()
    inputs = pair.publish(pair.output.with_name("resumed-final-push"))
    current = PullRequestHead.at(pair.core.github, pair.carrier_number)
    assert current.head == captured
    inputs.validate(pair.context.repos, current)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == captured
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_publication_recovery_refuses_changed_companion_without_recreating(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("Reply lost after creation")
    with pytest.raises(TodoError):
        pair.publish(pair.output)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    changed = pair.client.publish_change("Move the original source head", *pair.client.write_files({
        Path("src/workflow.py"): 'def run() -> str:\n    return "different source cut"\n',
    }))
    stamp = "2026-10-24T00:00:00Z"
    pair.github.put(pair.client.github, DraftListedPullRequest(
        pair.client.number, "feature", changed, stamp, "main", reviewable_times=(stamp,)))
    with pytest.raises(integration.IntegrationFailed):
        pair.publish(pair.output.with_name("changed-source-refusal"))
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_first_publication_waits_only_for_known_previous_forge_head(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("", lag_reads=2)
    inputs = pair.publish(pair.output)
    current = PullRequestHead.at(pair.core.github, pair.carrier_number)
    inputs.validate(pair.context.repos, current)
    assert current.head == pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_published_head_observation_refuses_an_unexpected_foreign_replacement(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("")
    inputs = pair.publish(pair.output)
    expected = PullRequestHead.at(pair.core.github, pair.carrier_number).identity
    stamp = "2026-10-24T00:00:00Z"
    replacement = "f" * 40
    pair.github.put(pair.core.github, DraftListedPullRequest(
        pair.carrier_number, "publish-carrier", replacement, stamp, "main",
        reviewable_times=(stamp,)))
    with pytest.raises(ToolRefusedError, match=replacement):
        expected.published_after(inputs.carrier)
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


@pytest.mark.parametrize("advance_core", (False, True))
@pytest.mark.parametrize("lost_reply", (False, True))
def test_explicit_recapture_preserves_real_carrier_and_old_history_after_changed_mains(
    mutual_sources: MutualSources, advance_core: bool, lost_reply: bool,
) -> None:
    pair = mutual_sources.starting_publication("Reply lost after creation" if lost_reply else "")
    with pytest.raises(TodoError) if lost_reply else nullcontext():
        pair.publish(pair.output)
    old = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    original_input = tuple(path.read_bytes() for path in pair.output.glob("*input.json"))
    sources = (pair.client, pair.core) if advance_core else (pair.client,)
    pair.advance_main(*sources)
    with pytest.raises(ToolRefusedError):
        pair.publish(pair.output.with_name("changed-cut-refusal"))
    carrier = PullRequestHead.at(pair.core.github, pair.carrier_number)
    output = pair.output.with_name("explicit-recapture")
    inputs = integration.IntegrationRun(pair.checkout, output, "Replace changed mains explicitly").recapture(
        pair.context, ("P3",), carrier=carrier.identity)
    current = PullRequestHead.at(pair.core.github, pair.carrier_number)
    inputs.validate(pair.context.repos, current)
    assert inputs.carrier.pr == carrier.pr == pair.carrier_number
    assert inputs.source(pair.client.github).baseline.commit == pair.client.main_commit
    assert inputs.local.baseline.commit == pair.core.main_commit
    assert not inputs.local.has_changes(pair.core)
    assert pair.core.git(pair.checkout.path, "show", "-s", "--format=%P", current.head).split() == [inputs.carrier.head]
    assert Repo.descends(pair.checkout.path, old, current.head)
    assert tuple(path.read_bytes() for path in pair.output.glob("*input.json")) == original_input
    assert pair.publish(pair.output.with_name("recovered-recapture")) == inputs
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == current.head
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


@pytest.mark.parametrize("borrowed", (False, True))
def test_explicit_recapture_refuses_wrong_holder_or_borrowed_checkout_before_mutation(
    mutual_sources: MutualSources, borrowed: bool,
) -> None:
    pair = mutual_sources.starting_publication("")
    pair.publish(pair.output)
    creator_checkout = pair.owned_checkout(pair.context, pair.core, "colleague", "P3")
    pair.core.git(creator_checkout.path, "switch", "-c", "creator-recapture")
    context = pair.context if borrowed else pair.context.model_copy(update={"actor": PeerCaller("colleague")})
    before = pair.core.git(creator_checkout.path, "rev-parse", "HEAD").strip()
    refs = pair.core.git(pair.core.remote_path, "show-ref")
    output = pair.output.with_name("forbidden-recapture")
    carrier = PullRequestHead.at(pair.core.github, pair.carrier_number)
    with pytest.raises(StorageDenied if borrowed else TodoConflict):
        integration.IntegrationRun(creator_checkout, output, "Refuse foreign recapture").recapture(
            context, ("P3",), carrier=carrier.identity)
    assert pair.core.git(creator_checkout.path, "rev-parse", "HEAD").strip() == before
    assert pair.core.git(pair.core.remote_path, "show-ref") == refs
    assert not output.exists()
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_explicit_recapture_rechecks_scope_before_integrating_core_main(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("")
    pair.publish(pair.output)
    pair.advance_main(pair.core)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    refs = pair.core.git(pair.core.remote_path, "show-ref")
    carrier = PullRequestHead.at(pair.core.github, pair.carrier_number)
    Team.on_root("colleague", (pair.client,), pair.context.root)
    output = pair.output.with_name("revoked-scope-recapture")
    with pytest.raises(ToolRefusedError):
        integration.IntegrationRun(pair.checkout, output, "Refuse revoked Core scope").recapture(
            pair.context, ("P3",), carrier=carrier.identity)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert pair.core.git(pair.core.remote_path, "show-ref") == refs
    assert not output.exists()
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_explicit_recapture_preserves_reused_output_before_integrating_main(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("")
    pair.publish(pair.output)
    pair.advance_main(pair.core)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    inputs = IntegrationInput.at(pair.output)
    carrier = PullRequestHead.at(pair.core.github, pair.carrier_number)
    with pytest.raises(FileExistsError):
        integration.IntegrationRun(pair.checkout, pair.output, "Preserve existing output").recapture(
            pair.context, ("P3",), carrier=carrier.identity)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert IntegrationInput.at(pair.output) == inputs
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


@pytest.mark.parametrize(("problems", "failure"), (
    (("P3", "P3"), integration.StopAfterFailure()),
    (("P3",), integration.ExcludeAfterFailure(7)),
))
def test_explicit_recapture_rejects_invalid_preparation_before_integrating_main(
    mutual_sources: MutualSources, problems: tuple[str, ...], failure: integration.FailureAction,
) -> None:
    pair = mutual_sources.starting_publication("")
    pair.publish(pair.output)
    pair.advance_main(pair.core)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    output = pair.output.with_name("invalid-preparation")
    carrier = PullRequestHead.at(pair.core.github, pair.carrier_number)
    with pytest.raises(integration.IntegrationFailed):
        integration.IntegrationRun(pair.checkout, output, "Refuse invalid preparation", failure).recapture(
            pair.context, problems, carrier=carrier.identity)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert not output.exists()
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_explicit_recapture_refuses_a_different_actual_carrier_branch(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("Reply lost after creation")
    with pytest.raises(TodoError):
        pair.publish(pair.output)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    stamp = "2026-10-24T00:00:00Z"
    foreign = PullRequestHead(pair.core.github, pair.carrier_number + 1, before)
    pair.github.put(pair.core.github, DraftListedPullRequest(
        foreign.pr, "another-carrier", before, stamp, "main", reviewable_times=(stamp,)))
    refs = pair.core.git(pair.core.remote_path, "show-ref")
    output = pair.output.with_name("different-carrier-refusal")
    with pytest.raises(integration.IntegrationFailed):
        integration.IntegrationRun(pair.checkout, output, "Refuse a different real carrier").recapture(
            pair.context, ("P3",), carrier=foreign)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert pair.core.git(pair.core.remote_path, "show-ref") == refs
    assert not output.exists()
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_explicit_recapture_cli_uses_same_real_carrier_without_creation(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("")
    pair.publish(pair.output)
    pair.advance_main(pair.client)
    output = pair.output.with_name("cli-recapture")
    result = subprocess.run((
        str(Path(sys.executable).with_name("agent-comms-integrate")),
        "--recapture", "P3", "--from", "worker", "--comms-root", str(pair.context.root),
        "--carrier", str(pair.carrier_number), "--checkout", str(pair.checkout.path),
        "--output", str(output), "--risk", "Exercise the explicit recapture console",
    ), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    inputs = IntegrationInput.at(output)
    inputs.validate(pair.context.repos, PullRequestHead.at(pair.core.github, pair.carrier_number))
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_prepared_attempt_without_discoverable_carrier_never_repeats_creation(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources.starting_publication("")
    prepared = integration.IntegrationRun(pair.checkout, pair.output, "Interrupted publication").prepare(
        pair.context, ("P3",))
    pair.core.git(pair.checkout.path, "commit", "--allow-empty", "-m", prepared.trailer)
    before = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    with pytest.raises(integration.IntegrationFailed):
        pair.publish(pair.output.with_name("missing-carrier-refusal"))
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip() == before
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 0


def test_publication_cli_uses_explicit_actor_and_real_source_cut(
    mutual_sources: MutualSources, tmp_path: Path,
) -> None:
    pair = mutual_sources.starting_publication("")
    body = tmp_path / "publication-body.md"
    body.write_text("The real companion migration supplies the substantive source change.\n")
    command = (str(Path(sys.executable).with_name("agent-comms-integrate")),
               "--publish", "P3", "--from", "worker", "--comms-root", str(pair.context.root),
               "--checkout", str(pair.checkout.path), "--output", str(pair.output),
               "--risk", "Exercise the actual explicit-actor publication entry",
               "--title", "Publish the selected companion source", "--body-file", str(body))
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    inputs = IntegrationInput.at(pair.output)
    inputs.validate(pair.context.repos, PullRequestHead.at(pair.core.github, pair.carrier_number))
    assert pair.context.comms.goals.todos.get("P3").held().owner == "worker"
    assert sum(call[:2] == ("pr", "create") for call in pair.github.calls()) == 1


def test_companion_source_cut_prepares_before_any_carrier_is_published(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources
    prepared = integration.IntegrationRun(
        pair.checkout, pair.output, "Prepare the real source cut before first publication"
    ).prepare(pair.context, ("P3",))
    assert prepared.predecessor.repository == pair.core.github
    assert prepared.predecessor.commit == pair.core.main_commit
    assert prepared.local.heads == ()
    assert not prepared.local.has_changes(pair.core)
    assert prepared.source(pair.client.github).has_changes(pair.client)
    assert IntegrationInput.from_canonical(prepared.canonical) == prepared
    source = PullRequestHead(pair.core.github, pair.core.number, pair.core.branch_commit)
    with pytest.raises(ToolRefusedError):
        prepared.require_carrier(pair.core, source)


def test_companion_only_capture_preserves_native_review_and_empty_test_selection(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
) -> None:
    pair = mutual_sources
    inputs = pair.capture(("P3",), "client-only change without a ceremonial Core patch")
    carrier = pair.publish_carrier(github_cli)
    inputs.validate(pair.context.repos, carrier)
    assert inputs.local.heads == ()
    assert not inputs.local.has_changes(pair.core)
    with pytest.raises(ToolRefusedError, match="no accepted exact-head source review"):
        MergeHeadReview.require_combined(carrier, pair.context.repos)
    assert pair.checkout.test_files(inputs.local.baseline.commit, inputs.local.heads) == ()
    with pytest.raises(integration.IntegrationFailed):
        integration.IntegrationRun(pair.checkout, pair.output, "An empty union cannot run a suite").test(
            inputs.local.baseline.commit, inputs.local.heads, TestLease.default_start, 1)


def test_qualification_refuses_an_entirely_unchanged_source_set(
    mutual_sources: MutualSources,
) -> None:
    pair = mutual_sources
    primary = pair.context.comms.goals.todos.get("P2")
    creation = CreateProblemGoalAction(
        problem="P4", request=primary.request,
        assigner=pair.context.comms.registry.require("colleague").incarnation,
        repo=pair.core.github, text="Check a source selection without changed members",
        # held_bus's own source is no longer a team repository once source_pair registers
        # core and client, so P4 checks the core checkout its repo names.
        order=replace(primary.order.checked(), pull_requests=(), check=replace(
            primary.order.checked().check, directory=pair.core.checkout)),
        review_at=RecordedEventTime(9999999999.0))
    PeerCaller("worker").invoke(
        CommsTodoCreateTool(owner="worker", order=creation), pair.context.comms)
    with pytest.raises(ToolRefusedError, match="captured sources have no substantive change"):
        pair.capture(("P4",), "unchanged sources must not acquire a capture")
    assert not (pair.output / IntegrationInput.filename).exists()
    assert IntegrationInput.from_message(Repo.git(
        pair.checkout.path, "show", "--format=%B", "--no-patch", "HEAD")) == ()


def test_real_mutual_imports_work_for_both_matched_cuts_and_fail_for_both_mixed_cuts(
        mutual_sources: MutualSources) -> None:
    import sys

    pair = mutual_sources
    pair.capture(("P2", "P3"), "both mixed imports must fail")
    results = {}
    for core_side in (BaseSide, HeadSide):
        for client_side in (BaseSide, HeadSide):
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join((
                str(core_side.checkout(pair.output, pair.core.github) / "src"),
                str(client_side.checkout(pair.output, pair.client.github) / "src"),
            ))}
            result = subprocess.run(
                (sys.executable, "-c", "from workflow import run; print(run())"),
                cwd=pair.output, env=environment, text=True, capture_output=True, timeout=30,
            )
            results[core_side, client_side] = result
    assert results[BaseSide, BaseSide].returncode == 0
    assert results[BaseSide, BaseSide].stdout.strip() == "baseline"
    assert results[HeadSide, HeadSide].returncode == 0
    assert results[HeadSide, HeadSide].stdout.strip() == "candidate"
    assert results[BaseSide, HeadSide].returncode != 0
    assert results[HeadSide, BaseSide].returncode != 0


@pytest.mark.parametrize("problems", (("P2", "P3"), ("P3",)))
def test_changed_main_during_source_review_refuses_the_frozen_input(
        mutual_sources: MutualSources, github_cli: GitHubFixture,
        problems: tuple[str, ...]) -> None:
    pair = mutual_sources
    run = integration.IntegrationRun(pair.checkout, pair.output, "stale baseline must refuse")
    inputs = pair.capture(problems, run.risk)
    carrier = pair.publish_carrier(github_cli)
    pair.client.git(pair.client.checkout, "checkout", "main")
    pair.client.commit_change("Advance client main", *pair.client.write_files({Path("main-only.py"): "advanced = True\n"}))
    pair.client.git(pair.client.checkout, "push", "origin", "main")
    with pytest.raises(ToolRefusedError, match="main moved"):
        inputs.validate(pair.context.repos, carrier)


def test_common_runtime_contract_allows_local_source_and_metadata_changes() -> None:
    baseline = ('[project]\nname="old-label"\nversion="1"\n'
                'dependencies=["agent-comms", "old-client", "packaging>=25"]\n[tool.uv]\npackage=false\n'
                '[tool.uv.sources]\nagent-comms={path="..",editable=true}\n'
                'old-client={git="https://github.com/example/old.git",branch="main"}\n')
    candidate = ('[project]\nname="new-label"\nversion="2"\n'
                 'dependencies=["agent-comms", "new-client", "packaging>=25"]\n[tool.uv]\npackage=false\n'
                 '[tool.uv.sources]\nagent-comms={path="..",editable=true}\n'
                 'new-client={git="https://github.com/example/new.git",branch="main"}\n')
    before, after = (PrivateUvProject.decode(text) for text in (baseline, candidate))
    StackSources.require_compatible(before, after)
    StackSources.require_compatible(after, before)


def test_common_runtime_contract_refuses_unsupported_external_dependency_migration() -> None:
    baseline = '[project]\ndependencies=["packaging>=25"]\n[tool.uv]\npackage=false\n'
    candidate = '[project]\ndependencies=["packaging>=26"]\n[tool.uv]\npackage=false\n'
    with pytest.raises(ValueError):
        StackSources.require_compatible(
            PrivateUvProject.decode(baseline), PrivateUvProject.decode(candidate)
        )


def test_main_checkout_is_refused_without_changes(tmp_path: Path):
    source = SourceRepository.create(tmp_path / "main-source")
    before = source.main_commit
    with pytest.raises(integration.IntegrationFailed, match="isolated Git worktree"):
        integration.IntegrationCheckout(source.checkout).require_editable()
    assert source.main_commit == before


def test_failure_does_not_guess_a_head_and_explicit_exclusion_keeps_the_remainder(
    heads, tmp_path: Path,
):
    log = tmp_path / "failed.log"
    with pytest.raises(integration.IntegrationFailed, match="No head is blamed"):
        integration.StopAfterFailure().remaining(heads, log)
    exclusion = integration.ExcludeAfterFailure(heads[0].pr)
    exclusion.require_heads(heads)
    assert exclusion.remaining(heads, log) == heads[1:]
    with pytest.raises(integration.IntegrationFailed, match="leaves no heads"):
        exclusion.remaining(heads[:1], log)
    with pytest.raises(integration.IntegrationFailed, match="not in"):
        integration.ExcludeAfterFailure(99).require_heads(heads)


def test_inherited_git_context_cannot_redirect_the_checkout_and_auth_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    requested = SourceRepository.create(tmp_path / "requested")
    foreign = SourceRepository.create(tmp_path / "foreign")
    monkeypatch.setenv("GIT_DIR", str(foreign.checkout / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign.checkout))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign.checkout / ".git/index"))
    monkeypatch.setenv("GIT_CONFIG", str(foreign.checkout / ".git/config"))
    monkeypatch.setenv("GIT_SSH_COMMAND", "controlled-auth-helper")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    assert Path(Repo.git(requested.checkout, "rev-parse", "--show-toplevel").strip()) == requested.checkout
    key, value = SETTINGS[0]
    assert Repo.git(requested.checkout, "config", "--get", key).strip() == value
    environment = Repo.environment(os.environ)
    assert environment["GIT_SSH_COMMAND"] == "controlled-auth-helper"
    assert environment["SSH_AUTH_SOCK"] == str(tmp_path / "agent.sock")


def test_success_finishes_without_entering_retry(heads, tmp_path: Path):
    run = integration.IntegrationRun(integration.IntegrationCheckout(tmp_path / "tree"),
                                     tmp_path / "output", "bounded completion")
    answer = integration.TestAttempt.from_process(
        subprocess.CompletedProcess(("pytest",), 0), tmp_path / "passed.log",
    )
    assert answer.finish(run, "a" * 40, heads) == 0
    assert not run.output.exists()
    failed = integration.TestAttempt.from_process(
        subprocess.CompletedProcess(("pytest",), 1), tmp_path / "failed.log",
    )
    assert failed.code == 1
    timed_out = integration.TestAttempt.from_process(
        subprocess.CompletedProcess(("timeout",), integration.TimedOutTestAttempt.exit_code),
        tmp_path / "timed-out.log",
    )
    with pytest.raises(integration.IntegrationFailed, match="no automatic replay"):
        timed_out.finish(run, "a" * 40, heads)
