"""Integration policy at real TOML/Git/gh boundaries; no patched Core methods."""

from collections.abc import Callable
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import tomli_w

from agent_comms import integration
from agent_comms.errors import ToolRefusedError
from agent_comms.head_review import BaseSide, CombinedInput, HeadReview, HeadSide, IntegrationInput, MergeHeadReview, PromotedHeadReview, PullRequestHead, ResolvedIntegrationSource
from agent_comms.repository_name import RepositoryName
from agent_comms.repo import Repo
from agent_comms.todos import (
    DraftListedPullRequest, MergedListedPullRequest, PullRequestCommit, QualifiedHeadVerdict,
    ReviewedHeadVerdict, UnmergedListedPullRequest, VerdictReason,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.installer.runtime_resolution import StackSources
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources
from source_revision_fixture import SourceRepository
from fixture_git import ENVIRONMENT, SETTINGS
from debt_ratchet import ClientTypeErrors, compare
from module_layers import MaterializedClients, Package

pytest_plugins = ("github_fixture", "test_holder_standing_rules", "integration_source_fixture")


@pytest.fixture
def heads() -> tuple[PullRequestHead, ...]:
    repo = RepositoryName.from_wire("example/integration")
    return (PullRequestHead(repo, 7, "a" * 40), PullRequestHead(repo, 8, "b" * 40))


@pytest.mark.parametrize("event", ("pull_request", "merge_group"))
def test_actual_ci_cli_uses_verified_carrier_not_synthetic_message_and_revalidates(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
    tmp_path: Path, event: str,
) -> None:
    pair = mutual_sources
    inputs = pair.capture(("P2", "P3"), "CI source graph")
    carrier = pair.publish_carrier(github_cli)
    local = inputs.source(pair.core.github)
    # Actual synthetic merge: its own message deliberately has no source metadata.
    tested = pair.core.git(pair.core.checkout, "commit-tree", local.candidate_tree,
                           "-p", local.baseline.commit, "-p", carrier.head,
                           "-m", "Synthetic PR/queue merge; no input metadata").strip()
    checkout = integration.IntegrationCheckout.create(pair.core, tmp_path / "ci-checkout", tested)
    assert IntegrationInput.from_message(Repo.git(checkout.path, "show", "--no-patch", "--format=%B", "HEAD")) == ()
    documents = {
        "pull_request": {"number": carrier.pr, "pull_request": {"head": {"sha": carrier.head}}},
        "merge_group": {"merge_group": {"head_sha": tested, "base_sha": local.baseline.commit,
                                        "head_ref": f"refs/heads/gh-readonly-queue/main/pr-{carrier.pr}-{carrier.head}"}},
    }
    document = tmp_path / "event.json"
    document.write_text(json.dumps(documents[event]))
    output = tmp_path / "resolved"
    environment = {**os.environ, **ENVIRONMENT, "GITHUB_EVENT_NAME": event,
                   "GITHUB_EVENT_PATH": str(document), "GITHUB_SHA": tested,
                   "GITHUB_REPOSITORY": pair.core.github.to_wire(), "RUNNER_TEMP": str(tmp_path),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    command = (sys.executable, "-m", "agent_comms.integration")
    workflow_output = tmp_path / "workflow-output"
    result = subprocess.run((*command, "--resolve-ci-inputs", str(output),
                             "--workflow-output", str(workflow_output)), cwd=checkout.path,
                            env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert workflow_output.read_text() == f"base={local.baseline.commit}\ncombined=true\n"
    assert IntegrationInput.at(output) == inputs
    assert inputs.carrier.pr == carrier.pr != pair.core.number
    assert local.heads == (PullRequestHead(pair.core.github, pair.core.number, pair.core.branch_commit),)
    assert pair.core.git(pair.checkout.path, "rev-parse", "HEAD^").strip() == inputs.carrier.head
    for source in inputs.sources:
        for side in (BaseSide, HeadSide):
            side.require_materialized(output, source)
    original = pair.core.git(pair.core.checkout, "rev-parse", "HEAD").strip()
    result = subprocess.run((*command, "--revalidate-ci-inputs", str(output)), cwd=checkout.path,
                            env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    pair.client.git(pair.client.checkout, "push", "origin", "HEAD:main")
    stale = subprocess.run((*command, "--revalidate-ci-inputs", str(output)), cwd=checkout.path,
                           env=environment, capture_output=True, text=True, timeout=120)
    assert stale.returncode != 0 and "main moved" in stale.stderr
    assert pair.core.git(pair.core.checkout, "rev-parse", "HEAD").strip() == original
    assert not any(call[:2] == ("pr", "merge") or "state=success" in call for call in github_cli.calls())


def test_actual_ordinary_ci_cli_captures_main_once_and_never_steals_an_unlinked_overlay(
    mutual_sources: MutualSources, github_cli: GitHubFixture, tmp_path: Path,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    head = pair.core.branch_commit
    baseline = pair.core.main_commit
    tree = pair.core.git(pair.core.checkout, "rev-parse", f"{head}^{{tree}}").strip()
    tested = pair.core.git(pair.core.checkout, "commit-tree", tree, "-p", baseline,
                           "-p", head, "-m", "Actual ordinary synthetic merge").strip()
    checkout = integration.IntegrationCheckout.create(pair.core, tmp_path / "ordinary-ci", tested)
    document = tmp_path / "ordinary-event.json"
    document.write_text(json.dumps({"number": pair.core.number, "pull_request": {"head": {"sha": head}}}))
    output = tmp_path / "ordinary-inputs"
    environment = {**os.environ, **ENVIRONMENT, "GITHUB_EVENT_NAME": "pull_request",
                   "GITHUB_EVENT_PATH": str(document), "GITHUB_SHA": tested,
                   "GITHUB_REPOSITORY": pair.core.github.to_wire(), "RUNNER_TEMP": str(tmp_path),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    workflow_output = tmp_path / "ordinary-workflow-output"
    result = subprocess.run((sys.executable, "-m", "agent_comms.integration", "--resolve-ci-inputs", str(output),
                             "--workflow-output", str(workflow_output)),
                            cwd=checkout.path, env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert workflow_output.read_text() == f"base={baseline}\ncombined=false\n"
    inputs = IntegrationInput.at(output)
    core = inputs.source(pair.core.github)
    client = inputs.source(pair.client.github)
    assert core.baseline.commit == baseline
    assert core.heads == (PullRequestHead(pair.core.github, pair.core.number, head),)
    assert client.heads == () and client.baseline.commit == pair.client.main_commit
    resource = materialized_clients(output, inputs)
    base = BaseSide.require_materialized(output, core)
    candidate = HeadSide.require_materialized(output, core)
    assert Package(base, resource.at(checkout.path, baseline, tested, BaseSide)).missing_names == ()
    assert {name for _, name in Package(candidate, resource.at(checkout.path, baseline, tested, HeadSide)).missing_names} == {"old_api"}
    assert not any("state=success" in call or call[:2] == ("pr", "merge") for call in github_cli.calls())
    refused = subprocess.run((sys.executable, "-m", "agent_comms.integration", "--check-merge-inputs",
                              str(tmp_path / "refused-individual")), cwd=checkout.path,
                             env=environment, capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0 and "individual source" in refused.stderr
    document.write_text(json.dumps({"merge_group": {
        "head_sha": tested, "base_sha": baseline,
        "head_ref": f"refs/heads/gh-readonly-queue/main/pr-{pair.core.number}-{head}"}}))
    queued = subprocess.run((sys.executable, "-m", "agent_comms.integration", "--check-merge-inputs",
                             str(tmp_path / "refused-ordinary-queue")), cwd=checkout.path,
                            env={**environment, "GITHUB_EVENT_NAME": "merge_group"},
                            capture_output=True, text=True, timeout=120)
    assert queued.returncode != 0 and "individual source" in queued.stderr
    assert not HeadReview.accepted(pair.core.github, tested)
    assert not MergeHeadReview.accepted(pair.core.github, tested)


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
                head.repo, head.head, "lead", "source")
    # A source review on the combined head is still not a merge promotion.
    ReviewedHeadVerdict(VerdictReason("The combined source is reviewed but not yet promoted for merge")).post(
        carrier.repo, carrier.head, "lead", "combined source")
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
    environment = {**os.environ, **ENVIRONMENT, "GITHUB_EVENT_NAME": event,
                   "GITHUB_EVENT_PATH": str(document), "GITHUB_SHA": tested,
                   "GITHUB_REPOSITORY": pair.core.github.to_wire(), "RUNNER_TEMP": str(tmp_path),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    command = (sys.executable, "-m", "agent_comms.integration", "--check-merge-inputs")
    result = subprocess.run((*command, str(tmp_path / "before-promotion")), cwd=checkout.path,
                            env=environment, capture_output=True, text=True, timeout=120)
    assert (result.returncode == 0) == (event == "pull_request"), result.stdout + result.stderr
    if event == "merge_group":
        assert "no explicit reviewed merge promotion" in result.stderr
    ReviewedHeadVerdict(VerdictReason("The actual combined carrier is explicitly reviewed for merge")).post(
        carrier.repo, carrier.head, "lead", "source")
    PromotedHeadReview().post(carrier.repo, carrier.head, "lead", "promotion")
    promoted = subprocess.run((*command, str(tmp_path / "after-promotion")), cwd=checkout.path,
                              env=environment, capture_output=True, text=True, timeout=120)
    assert promoted.returncode == 0, promoted.stdout + promoted.stderr
    assert HeadReview.accepted(carrier.repo, tested) is (event == "merge_group")
    assert MergeHeadReview.accepted(carrier.repo, tested) is (event == "merge_group")
    member = inputs.source(pair.client.github).heads[0]
    QualifiedHeadVerdict(VerdictReason("This member is no longer accepted for the combined source")).post(
        member.repo, member.head, "lead", "withdraw member")
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
    primary = comms.goals.todos.get("P2")
    comms.goals.update_goal("worker", CreateProblemGoalAction(
        problem="P4", commander="lead", request=primary.request,
        assigner=comms.registry.require("lead").incarnation,
        repo=peer.github, text="Include the second existing companion source",
        order=replace(primary.order.checked(), pull_requests=(peer.link,),
                      check=replace(primary.order.check, directory=peer.checkout)),
        review_at=RecordedEventTime(9999999999.0)))
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
    primary = pair.context.comms.goals.todos.get("P2")
    pair.context.comms.goals.update_goal("worker", CreateProblemGoalAction(
        problem="P4", commander="lead", request=primary.request,
        assigner=pair.context.comms.registry.require("lead").incarnation,
        repo=peer.github, text="Include the existing local source",
        order=replace(primary.order.checked(), pull_requests=(peer.link,),
                      check=replace(primary.order.check, directory=peer.checkout)),
        review_at=RecordedEventTime(9999999999.0)))
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
    environment = {**os.environ, **ENVIRONMENT, "GITHUB_EVENT_NAME": "push",
                   "GITHUB_EVENT_PATH": str(document), "GITHUB_SHA": tested,
                   "GITHUB_REPOSITORY": pair.core.github.to_wire(), "RUNNER_TEMP": str(tmp_path),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    command = (sys.executable, "-m", "agent_comms.integration")
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
    assert len(ClientTypeErrors.parts(pair.core.checkout, source.baseline.commit, head,
                                     {"src/agent_comms/api.py"}, "src/", clients)) == 2
    for side in (BaseSide, HeadSide):
        client, = clients.at(pair.core.checkout, source.baseline.commit, head, side)
        assert client.import_paths == tuple(str(client.checkout / path) for path in ("src", "tools"))
        assert client.files_importing("agent_comms") == [
            "mypy_source_plugin.py", "src/workflow.py", "tools/acceptance.py", "tools/performance/capture_live.py"]
        # Python's script entrypoint and the plugin's import must see this same cut.
        entry = subprocess.run((sys.executable, "tools/acceptance.py"), cwd=client.checkout,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((
                str(side.require_materialized(pair.output, source) / "src"), *client.import_paths))},
            capture_output=True, text=True, timeout=30)
        assert entry.returncode == 0, entry.stdout + entry.stderr
    # Both namespace files are selected under --explicit-package-bases: a duplicate
    # performance/tools.performance identity aborts the real checker, not a zero count.
    result = compare(pair.core.checkout, source.baseline.commit, head, "src", 1,
                     measured=(ClientTypeErrors,), clients=clients)
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
            environment=integration.RunnerEnvironment(root=tmp_path))
    with pytest.raises(integration.IntegrationFailed, match="queued carrier"):
        integration.MergeGroupSources.resolve(pair.core, pair.core.main_commit,
            {"merge_group": {"head_sha": pair.core.main_commit, "base_sha": pair.core.main_commit,
                             "head_ref": "refs/heads/not-a-GitHub-queue-reference"}},
            environment=integration.RunnerEnvironment(root=tmp_path))
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


def test_stack_source_identity_normalizes_package_spelling_and_refuses_duplicate_aliases() -> None:
    source = '{git = "https://github.com/example/client.git", branch = "main"}'
    assert StackSources.declared_sources(f'[tool.uv.sources]\nSource_Client = {source}\n') == {
        "source-client": ("https://github.com/example/client.git", "main")}
    with pytest.raises(ValueError, match="declares package source-client twice"):
        StackSources.declared_sources(f'[tool.uv.sources]\nSource_Client = {source}\nsource-client = {source}\n')


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
        source.github, qualified, "lead", "source stage",
    )
    run = integration.IntegrationRun(integration.IntegrationCheckout(source.checkout),
                                     tmp_path / "output", "ready-head selection")
    assert run.accepted_heads() == ()
    ReviewedHeadVerdict(VerdictReason("The declared class owns the reviewed behavior")).post(
        source.github, source.branch_commit, "lead", "integration",
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


def test_changed_main_during_source_review_refuses_the_frozen_input(
        mutual_sources: MutualSources, github_cli: GitHubFixture) -> None:
    pair = mutual_sources
    run = integration.IntegrationRun(pair.checkout, pair.output, "stale baseline must refuse")
    inputs = pair.capture(("P2", "P3"), run.risk)
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
    StackSources.require_compatible(baseline, candidate)
    StackSources.require_compatible(candidate, baseline)


def test_common_runtime_contract_refuses_unsupported_external_dependency_migration() -> None:
    baseline = '[project]\ndependencies=["packaging>=25"]\n[tool.uv]\npackage=false\n'
    candidate = '[project]\ndependencies=["packaging>=26"]\n[tool.uv]\npackage=false\n'
    with pytest.raises(ValueError):
        StackSources.require_compatible(baseline, candidate)


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
