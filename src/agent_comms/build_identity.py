"""Installed repository commits, shared by delivery proofs and close checks."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import GitCommitText
from agent_comms.installer.runtime_resolution import RuntimePackage, RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName

if TYPE_CHECKING:
    from agent_comms.order_runs import OrderRun
    from agent_comms.todos import MergedProblemOrder, Todo


class ProofBuildMismatchError(ValueError):
    """A retained proof does not identify the currently installed repositories."""


class DeliveredCommitNotInstalledError(ValueError):
    """The installed repository does not contain the delivered change."""


class UninstalledRepositoryError(ValueError):
    """The checkout's repository is not carried by the recorded installation."""


class CommitInstallation(DeclaredFamily, affix="CommitInstallation"):
    """Whether the installed build contains a set of delivered commits."""

    merged_state: ClassVar[str]

    @abstractmethod
    def require(self) -> None: ...

    @abstractmethod
    def after_merge(self, todo: Todo, merged: MergedProblemOrder) -> Todo: ...

    def awaits_holder(self, run: OrderRun) -> bool:
        """Whether the merged order now waits on its holder rather than the commander's install."""
        return False


@dataclass(frozen=True)
class InstalledCommitInstallation(CommitInstallation):
    merged_state = "installed"

    def require(self) -> None:
        pass

    def awaits_holder(self, run: OrderRun) -> bool:
        return run.holder_runs()

    def after_merge(self, todo: Todo, merged: MergedProblemOrder) -> Todo:
        return merged.closed_in(todo)


@dataclass(frozen=True)
class UninstalledRepositoryCommitInstallation(InstalledCommitInstallation):
    """A repository the runtime never carries: its merge owes no install."""
    repository: RepositoryName
    merged_state = "no runtime install"


@dataclass(frozen=True)
class MissingCommitInstallation(CommitInstallation):
    commit: str
    installed: RecordedBuildIdentity
    merged_state = "waiting on install"

    def require(self) -> None:
        raise DeliveredCommitNotInstalledError(
            f"Delivered commit {self.commit} is not contained in installed {self.installed}."
        )

    def after_merge(self, todo: Todo, merged: MergedProblemOrder) -> Todo:
        return merged.waiting_in(todo)


@dataclass(frozen=True)
class RepositoryCommit:
    repository: RepositoryName
    commit: Annotated[str, GitCommitText]


class BuildIdentity(DeclaredFamily, affix="BuildIdentity"):
    @abstractmethod
    def recorded(self) -> RecordedBuildIdentity: ...

    @abstractmethod
    def installation(self, commits: tuple[str, ...], directory: Path) -> CommitInstallation: ...


@dataclass(frozen=True)
class UnrecordedBuildIdentity(BuildIdentity):
    def recorded(self) -> RecordedBuildIdentity:
        return RecordedBuildIdentity.installed()

    def installation(self, commits: tuple[str, ...], directory: Path) -> CommitInstallation:
        installed = RecordedBuildIdentity.installed()
        raise ProofBuildMismatchError(
            f"Proof build is unrecorded; installed {installed}; run and record a new proof."
        )


@dataclass(frozen=True)
class RecordedBuildIdentity(BuildIdentity):
    """Declared runtime source revisions, each identified by its repository."""

    commits: tuple[RepositoryCommit, ...]

    def __str__(self) -> str:
        return ", ".join(
            f"{record.repository.to_wire()} {record.commit}" for record in self.commits
        )

    def recorded(self) -> RecordedBuildIdentity:
        return self

    @classmethod
    def installed(cls) -> RecordedBuildIdentity:
        runtime = LaunchRuntime.installed()
        runtime.require_available()
        resolution = RuntimeResolution.at(runtime.prefix)
        return cls(
            tuple(
                RepositoryCommit(member.repository, GitCommitText.decode(member.commit(resolution)))
                for member in RuntimePackage.members_with(RuntimePackage)
            )
        )

    def for_repository(self, repository: Repo) -> RepositoryCommit:
        try:
            return next(record for record in self.commits if record.repository == repository.github)
        except StopIteration as error:
            raise UninstalledRepositoryError(
                f"Repository {repository.github.to_wire()} at {repository.checkout} is not in installed {self}."
            ) from error

    def containing(self, commits: tuple[str, ...], directory: Path) -> CommitInstallation:
        """The first commit this build lacks, read against the checkout's installed revision."""
        repository = Repo.of(directory).github
        return next(
            (next((MissingCommitInstallation(commit, self) for commit in commits
                   if not Repo.descends(directory, commit, record.commit)),
                  InstalledCommitInstallation())
             for record in self.commits if record.repository == repository),
            UninstalledRepositoryCommitInstallation(repository),
        )

    def contains(self, commit: str, directory: Path) -> bool:
        """Prove ancestry against this checkout's installed repository, not its current head."""
        recorded = self.for_repository(Repo.of(directory))
        return Repo.descends(directory, commit, recorded.commit)

    def installation(self, commits: tuple[str, ...], directory: Path) -> CommitInstallation:
        installed = self.installed()
        if self != installed:
            raise ProofBuildMismatchError(
                f"Proof build {self} differs from installed {installed}; run the proof again."
            )
        return installed.containing(commits, directory)
