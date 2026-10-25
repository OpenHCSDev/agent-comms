"""Integration policy at real TOML/Git/gh boundaries; no patched Core methods."""

import os
from pathlib import Path
import subprocess

import pytest

from agent_comms import integration
from agent_comms.head_review import PullRequestHead
from agent_comms.repository_name import RepositoryName
from agent_comms.repo import Repo
from agent_comms.test_lease import NewTestRun
from agent_comms.todos import (
    DraftListedPullRequest, ReviewedHeadVerdict, UnmergedListedPullRequest, VerdictReason,
)
from source_revision_fixture import SourceRepository
from fixture_git import ENVIRONMENT, SETTINGS

pytest_plugins = ("github_fixture",)


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
    run = integration.IntegrationRun(integration.IntegrationCheckout(source.checkout),
                                     tmp_path / "output", "ready-head selection")
    assert run.accepted_heads() == ()
    ReviewedHeadVerdict(VerdictReason("The declaration owns this behavior")).post(
        source.github, source.branch_commit, "lead", "integration",
    )
    assert run.accepted_heads() == (PullRequestHead(source.github, 7, source.branch_commit),)


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
    assert answer.finish(run, "a" * 40, heads, NewTestRun()) == 0
    assert not run.output.exists()
    failed = integration.TestAttempt.from_process(
        subprocess.CompletedProcess(("pytest",), 1), tmp_path / "failed.log",
    )
    assert failed.code == 1
