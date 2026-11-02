"""Every Git launch passes --no-lazy-fetch, so the boundary refuses a Git that predates it."""

from pathlib import Path

import pytest

from agent_comms.native_build_inputs import GitLaunch, GitVersion, GitVersionUnsupported


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


def test_git_older_than_the_declared_minimum_is_refused_by_name(tmp_path: Path) -> None:
    program = reporting_git(tmp_path / "old", "git version 2.43.0")
    with pytest.raises(GitVersionUnsupported) as refused:
        GitLaunch.environment({"PATH": str(program.parent)})
    assert (refused.value.found, refused.value.required) == (
        GitVersion(2, 43, 0), GitLaunch.minimum_version)
    assert refused.value.program == program
    assert "2.43.0" in str(refused.value) and "2.44.0" in str(refused.value)


@pytest.mark.parametrize("report", ("git version 2.44.0", "git version 2.47.1 (Apple Git-154)"))
def test_git_at_or_after_the_minimum_proceeds(tmp_path: Path, report: str) -> None:
    program = reporting_git(tmp_path / "new", report)
    environment = GitLaunch.environment({"PATH": str(program.parent), "HOME": str(tmp_path)})
    assert environment["LC_ALL"] == "C"


@pytest.mark.parametrize("report", ("2.44.0", "git version two"))
def test_an_unreadable_version_report_is_refused(report: str) -> None:
    with pytest.raises(ValueError):
        GitVersion.reported(report)
