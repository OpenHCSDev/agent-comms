"""Order check commands, expected outputs, and commit-note check records."""

from __future__ import annotations

import getopt
import os
import signal
import subprocess
import tempfile
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.execution.child_process import (
    BoundedRun,
    CheckPrinted,
    ChildOutcome,
    ChildResult,
    CountPrinted,
    ExitedOutcome,
    SignaledOutcome,
    SpawnedProcess,
    TimedOutOutcome,
)
from agent_comms.installer.native_build_inputs import ConfiguredChildStdio
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, PathText
from agent_comms.projects.change_report import CitationProject
from agent_comms.projects.checkouts import DetachedStart, ScratchCheckout
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo
from agent_comms.projects.shell_effects import shell_storage_effects
from agent_comms_hooks.shell_commands import ShellCommand


class CheckResult(DeclaredFamily, affix="CheckResult"):
    def require(self, result: ChildResult) -> None:
        self.distance(result).require(self)

    @abstractmethod
    def distance(self, result: ChildResult) -> CheckPrinted:
        """The measured distance, or the run's declared missing-count state."""


@dataclass(frozen=True)
class SilentCheckResult(CheckResult):
    def distance(self, result: ChildResult) -> CheckPrinted:
        return CountPrinted(int(result.outcome != ExitedOutcome(0) or bool(result.stdout)))


@dataclass(frozen=True)
class CountCheckResult(CheckResult):
    expected: int

    def __post_init__(self):
        if self.expected < 0:
            raise ValueError("An expected count must be nonnegative.")

    def distance(self, result: ChildResult) -> CheckPrinted:
        return result.outcome.printed_count(result.stdout).distance(self.expected)


class CheckCommand:
    """Command validation shared by authored requests and stored checks."""

    command: str

    def __post_init__(self):
        if not self.command.strip():
            raise ValueError("An order check requires a command.")

    def require_writable(self) -> None:
        """New checks cannot select revisions; saved checks remain readable for correction."""
        self.require_order_effects(Path.cwd())
        if any(self.git_command(list(simple.words)) in self.REVISION_COMMANDS
               for simple in ShellCommand.split(self.command)):
            raise ValueError("A check never checks out a revision: Core runs it at the "
                             "delivered commit and at each branch commit. Drop the checkout.")

    def require_order_effects(self, cwd: Path) -> None:
        for effect in shell_storage_effects(self.command, cwd):
            effect.require_order_check()

    # The revision is the run's parameter; a check line that chooses one is refused.
    REVISION_COMMANDS: ClassVar[frozenset[str]] = frozenset({"checkout", "switch"})

    @staticmethod
    def git_command(words: Sequence[str]) -> str:
        """The git subcommand ``words`` run, or "" when they run no git."""
        if not words or Path(words[0]).name != Repo.program:
            return ""
        try:
            selected = Repo.options(list(words[1:]))
        except getopt.GetoptError as error:
            raise ValueError(f"Incomplete or unknown git option: {error}") from error
        return next(iter(selected.arguments), "")


@dataclass(frozen=True)
class OrderCheck(CheckCommand):
    directory: Annotated[Path, PathText]
    command: str
    result: CheckResult = field(default_factory=SilentCheckResult)
    timeout: ClassVar[int] = 300

    def bound_in(self, project: CitationProject) -> OrderCheck:
        """The written directory as the project's registered checkout."""
        return replace(self, directory=project.checkout_named(str(self.directory)))

    def require_writable(self) -> None:
        """A relative directory means the cwd of whoever reads it next: the connector's
        heartbeat, a unit's checkout, a teammate in another shell. New checks bind it first."""
        super().require_writable()
        if not self.directory.is_absolute():
            raise ValueError(f"The order check directory {str(self.directory)!r} is relative; "
                             "name a registered repository so the sender binds its checkout.")

    def require_directory(self):
        if not self.directory.is_dir():
            raise ValueError("The order check directory does not exist.")

    def require_acceptance(self, base: str, checkout_base: Path) -> None:
        """The order state supplies the source preceding its delivered work."""
        self.require_writable()
        try:
            ran = self.run_at(base, self.directory, checkout_base)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"The order check timed out on {base}.") from error
        if refused := self.result.distance(ran).unproven(self.command, 0):
            raise ValueError("; ".join(refused))

    async def run(self) -> ChildResult:
        """The check on its directory's live state."""
        self.require_order_effects(self.directory)
        return await BoundedRun.run(("bash", "-c", self.command), cwd=self.directory,
                                    timeout=self.timeout)

    def recorded(self, sha: str, repository: Path) -> tuple[ChildResult, ...]:
        """Read the check run result without starting the check."""
        return self.recorded_among(CheckedCommit.stored(repository, sha))

    def recorded_among(self, stored: tuple[CheckedCommit, ...]) -> tuple[ChildResult, ...]:
        """This check's result among a commit's stored check results."""
        return tuple(checked.result for checked in stored if checked.command == self.command)[:1]

    def at(self, revision: str, repository: Path, checkout_base: Path) -> ChildResult:
        """Read the result on ``revision``, or explicitly run the check in a detached checkout."""
        sha = Repo.git(repository, "rev-parse", f"{revision}^{{commit}}").strip()
        for recorded in self.recorded(sha, repository):
            return recorded
        try:
            result = self.run_at(sha, repository, checkout_base)
        except subprocess.TimeoutExpired:
            return ChildResult(TimedOutOutcome(SignaledOutcome(signal.SIGKILL)))
        checked = CheckedCommit(
            self.command, result.outcome, result.stdout.decode(errors="replace"))
        checked.store(repository, sha)
        return checked.result

    def run_at(self, revision: str, repository: Path, checkout_base: Path) -> ChildResult:
        """Run in a fresh detached tree, never in the configured checkout."""
        self.require_order_effects(repository)
        checkout_base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="order-check-", dir=checkout_base) as scratch:
            tree = str(Path(scratch) / "tree")
            checkout = ScratchCheckout.add(repository, tree, DetachedStart(revision))
            try:
                run = SpawnedProcess.run(
                    ("bash", "-c", self.command), cwd=tree, timeout=self.timeout,
                    env=dict(os.environ),
                    stdio=ConfiguredChildStdio(stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                )
            finally:
                checkout.retire()
        return ChildResult(ChildOutcome.from_returncode(run.returncode), run.stdout)


@dataclass(frozen=True)
class CheckedCommit:
    """What an order's check printed at a commit. A commit's result never changes, so the git
    note that records it on the commit is read by every process before any check runs."""

    command: str
    outcome: ChildOutcome
    stdout: str

    NOTES: ClassVar[tuple[str, ...]] = (
        "-c", "user.name=agent-comms", "-c", "user.email=core@agent-comms.invalid",
        "notes", "--ref=agent-comms-checks")

    @property
    def result(self) -> ChildResult:
        return ChildResult(self.outcome, self.stdout.encode())

    @classmethod
    def stored(cls, repository: Path, sha: str) -> tuple[CheckedCommit, ...]:
        """Every check result the commit's note records; a commit no check ran on has none."""
        try:
            note = Repo.git(repository, *cls.NOTES, "show", sha)
        except GitCommandError as failure:
            if failure.no_check_ran_on(sha):
                return ()  # No check ran on this commit.
            raise
        return tuple(FieldCodec.decode(cls, FieldCodec.parse(line))
                     for line in note.splitlines() if line)

    def store(self, repository: Path, sha: str) -> None:
        Repo.git(repository, *self.NOTES, "append", "-m",
                 FieldCodec.compact(FieldCodec.encode(self)), sha)
