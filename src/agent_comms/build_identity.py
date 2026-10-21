"""Installed repository commits, shared by delivery proofs and close checks."""

from __future__ import annotations

import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import GitCommitText
from agent_comms.installer.runtime_resolution import RuntimePackage, RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName


class ProofBuildMismatchError(ValueError):
    """A retained proof does not identify the currently installed repositories."""


class DeliveredCommitNotInstalledError(ValueError):
    """The installed repository does not contain the delivered change."""


class UninstalledRepositoryError(ValueError):
    """The checkout's repository is not carried by the recorded installation."""


@dataclass(frozen=True)
class RepositoryCommit:
    repository: RepositoryName
    commit: Annotated[str, GitCommitText]


class BuildIdentity(DeclaredFamily, affix="BuildIdentity"):
    @abstractmethod
    def recorded(self) -> RecordedBuildIdentity: ...

    @abstractmethod
    def require_installed(self, commits: tuple[str, ...], directory: Path) -> None: ...


@dataclass(frozen=True)
class UnrecordedBuildIdentity(BuildIdentity):
    def recorded(self) -> RecordedBuildIdentity:
        return RecordedBuildIdentity.installed()

    def require_installed(self, commits: tuple[str, ...], directory: Path) -> None:
        installed = RecordedBuildIdentity.installed()
        raise ProofBuildMismatchError(
            f"Proof build is unrecorded; installed {installed}; run and record a new proof."
        )


@dataclass(frozen=True)
class RecordedBuildIdentity(BuildIdentity):
    """Every installed source revision, identified by its repository."""

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
        return cls.from_resolution(RuntimeResolution.at(runtime.prefix))

    @classmethod
    def from_resolution(cls, resolution: RuntimeResolution) -> RecordedBuildIdentity:
        return cls(
            tuple(
                RepositoryCommit(member.repository, GitCommitText.decode(member.commit(resolution)))
                for member in RuntimePackage.members_with(RuntimePackage)
            )
        )

    def for_repository(self, repository: Repo) -> RepositoryCommit:
        recorded = next(
            (record for record in self.commits if record.repository == repository.github), None
        )
        if recorded is None:
            raise UninstalledRepositoryError(
                f"Repository {repository.github.to_wire()} at {repository.checkout} is not in installed {self}."
            )
        return recorded

    def contains(self, commit: str, directory: Path) -> bool:
        """Prove ancestry against this checkout's installed repository, not its current head."""
        recorded = self.for_repository(Repo.of(directory))
        result = subprocess.run(
            ("git", "merge-base", "--is-ancestor", commit, recorded.commit),
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode not in (0, 1):
            result.check_returncode()
        return result.returncode == 0

    def require_installed(self, commits: tuple[str, ...], directory: Path) -> None:
        installed = self.installed()
        if self != installed:
            raise ProofBuildMismatchError(
                f"Proof build {self} differs from installed {installed}; run the proof again."
            )
        for commit in commits:
            if not installed.contains(commit, directory):
                raise DeliveredCommitNotInstalledError(
                    f"Delivered commit {commit} is not contained in installed {installed}."
                )
