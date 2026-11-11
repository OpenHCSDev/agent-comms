from __future__ import annotations

import subprocess
from pathlib import Path

from agent_comms.coordination.errors import CommandFailure


class SourceReadFailure(Exception):  # noqa: N818 - capability, not an outcome
    """A checkout, its Git history or its forge could not answer one source read.

    Every boundary that turns one source's failure into data catches this family, so a
    new failure kind joins the boundary by inheriting here, never by editing an except."""


class GitCommandError(RuntimeError, CommandFailure, SourceReadFailure):
    """An unreadable Git input; concrete failures own the evidence for narrower answers."""

    def source_path_absent(self, sha: str, file: str) -> bool:
        return False

    def no_such_revision(self) -> bool:
        return False

    def no_check_ran_on(self, sha: str) -> bool:
        return False

    def unrelated_history(self) -> bool:
        return False


class FailedGitCommand(GitCommandError):
    """A completed Git process owns its exit status and original stderr."""

    def __init__(self, repository: str, run: subprocess.CompletedProcess[bytes]):
        self.returncode = run.returncode
        self.stderr = run.stderr.decode(errors="replace")
        detail = self.stderr.strip() or f"git failed with exit {run.returncode}"
        super().__init__(f"Git in {repository}: {detail}")

    def source_path_absent(self, sha: str, file: str) -> bool:
        """Only Git's missing-path result, never a failed fetch or corrupt repository."""
        return self.returncode == 128 and self.stderr in (
            f"fatal: path '{file}' does not exist in '{sha}'\n",
            f"fatal: path '{file}' exists on disk, but not in '{sha}'\n",
        )

    def no_such_revision(self) -> bool:
        """``rev-parse --verify --quiet``'s unresolved revision, not a broken repository."""
        return self.returncode == 1 and not self.stderr

    def no_check_ran_on(self, sha: str) -> bool:
        """Git's absent-note reply, not a corrupt repository or an invalid revision."""
        return self.returncode == 1 and self.stderr == f"error: no note found for object {sha}.\n"

    def unrelated_history(self) -> bool:
        return self.returncode == 1


class UnavailableCommitError(GitCommandError):
    """Required Git objects are unavailable, not a negative ancestry or absent-path answer."""

    def __init__(self, checkout: Path | str, objects: tuple[str, ...], detail: str = ""):
        super().__init__(f"Git in {checkout}: required Git objects unavailable: "
                         f"{', '.join(objects)}" + (f"; {detail}" if detail else ""))
