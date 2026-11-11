"""Only a partial clone's reads pass --no-lazy-fetch, so only they refuse a Git that predates it."""

import subprocess
from pathlib import Path

import pytest

from agent_comms.installer.native_build_inputs import (
    CloneShape,
    FullClone,
    GitLaunch,
    GitVersion,
    GitVersionUnsupported,
    PartialClone,
)
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo


def reporting_git(directory: Path, report: str) -> Path:
    """A git on PATH that reports one version and answers the local-environment probe."""
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / "git"
    program.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{report}"; fi\n'
        "exit 0\n"
    )
    program.chmod(0o755)
    return program


def test_a_partial_clone_refuses_git_older_than_its_reads_need(tmp_path: Path) -> None:
    program = reporting_git(tmp_path / "old", "git version 2.43.0")
    with pytest.raises(GitVersionUnsupported) as refused:
        PartialClone().read_options(program)
    assert (refused.value.found, refused.value.required) == (
        GitVersion(2, 43, 0), PartialClone.minimum_version)
    assert refused.value.program == program
    assert "2.43.0" in str(refused.value) and "2.44.0" in str(refused.value)


def test_a_full_clone_reads_on_any_git(tmp_path: Path) -> None:
    program = reporting_git(tmp_path / "old", "git version 2.43.0")
    assert FullClone().read_options(program) == ()
    assert GitLaunch.environment({"PATH": str(program.parent), "HOME": str(tmp_path)})["LC_ALL"] == "C"


@pytest.mark.parametrize("report", ("git version 2.44.0", "git version 2.47.1 (Apple Git-154)"))
def test_a_partial_clone_on_a_recent_git_refuses_lazy_fetches(tmp_path: Path, report: str) -> None:
    assert PartialClone().read_options(reporting_git(tmp_path / "new", report)) == ("--no-lazy-fetch",)


@pytest.mark.parametrize("report", ("2.44.0", "git version two"))
def test_an_unreadable_version_report_is_refused(report: str) -> None:
    with pytest.raises(ValueError):
        GitVersion.reported(report)


def test_an_unreadable_checkout_fails_its_read_and_is_probed_again(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    config = tmp_path / ".git" / "config"
    readable = config.read_text()
    config.write_text(readable + "[unterminated\n")
    with pytest.raises(GitCommandError):
        Repo.git(tmp_path, "rev-parse", "--git-dir")
    config.write_text(readable)
    assert Repo.git(tmp_path, "rev-parse", "--git-dir").strip() == ".git"
    assert CloneShape.of(tmp_path) is CloneShape.shapes[tmp_path]
