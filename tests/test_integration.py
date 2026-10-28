"""Integration policy at real TOML/Git/gh boundaries; no patched Core methods."""

import os
from pathlib import Path
import subprocess

import pytest

from agent_comms import integration
from agent_comms.errors import ToolRefusedError
from agent_comms.head_review import BaseSide, HeadReview, HeadSide, IntegrationInput, PullRequestHead
from agent_comms.repository_name import RepositoryName
from agent_comms.repo import Repo
from agent_comms.todos import (
    DraftListedPullRequest, QualifiedHeadVerdict, ReviewedHeadVerdict, UnmergedListedPullRequest, VerdictReason,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.runtime_resolution import StackSources
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources
from source_revision_fixture import SourceRepository
from fixture_git import ENVIRONMENT, SETTINGS

pytest_plugins = ("github_fixture", "test_holder_standing_rules", "integration_source_fixture")


@pytest.fixture
def heads() -> tuple[PullRequestHead, ...]:
    repo = RepositoryName.from_wire("example/integration")
    return (PullRequestHead(repo, 7, "a" * 40), PullRequestHead(repo, 8, "b" * 40))


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
    inputs = run.qualify(pair.context, ("P2", "P3"))
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
    assert not any(call[:2] == ("pr", "merge") or "POST" in call for call in github_cli.calls())


def test_real_mutual_imports_work_for_both_matched_cuts_and_fail_for_both_mixed_cuts(
        mutual_sources: MutualSources) -> None:
    import sys

    pair = mutual_sources
    integration.IntegrationRun(pair.checkout, pair.output, "both mixed imports must fail").qualify(
        pair.context, ("P2", "P3"))
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
    inputs = run.qualify(pair.context, ("P2", "P3"))
    carrier = pair.publish_carrier(github_cli)
    pair.client.git(pair.client.checkout, "checkout", "main")
    pair.client.publish_change("Advance client main", *pair.client.write_files({Path("main-only.py"): "advanced = True\n"}))
    pair.client.git(pair.client.checkout, "push", "origin", "main")
    with pytest.raises(ToolRefusedError, match="main moved"):
        inputs.validate(pair.context.repos, carrier)


def test_common_runtime_contract_allows_local_source_and_metadata_changes() -> None:
    baseline = ('[project]\nname="old-label"\nversion="1"\n'
                'dependencies=["agent-comms", "old-client", "packaging>=25"]\n'
                '[tool.uv.sources]\nagent-comms={path="..",editable=true}\n'
                'old-client={git="https://github.com/example/old.git",branch="main"}\n')
    candidate = ('[project]\nname="new-label"\nversion="2"\n'
                 'dependencies=["agent-comms", "new-client", "packaging>=25"]\n'
                 '[tool.uv.sources]\nagent-comms={path="..",editable=true}\n'
                 'new-client={git="https://github.com/example/new.git",branch="main"}\n')
    StackSources.require_compatible(baseline, candidate)
    StackSources.require_compatible(candidate, baseline)


def test_common_runtime_contract_refuses_unsupported_external_dependency_migration() -> None:
    baseline = '[project]\ndependencies=["packaging>=25"]\n'
    candidate = '[project]\ndependencies=["packaging>=26"]\n'
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
