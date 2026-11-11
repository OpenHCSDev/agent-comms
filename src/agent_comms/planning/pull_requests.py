from __future__ import annotations

import calendar
import subprocess
import time
from abc import abstractmethod
from dataclasses import dataclass
from functools import cached_property
from typing import (
    ClassVar,
    Self,
)

from agent_comms.errors import TodoError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.change_report import FetchedRevision
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.work_branches import PageInfo, WorkBranches


@dataclass(frozen=True)
class PullRequestHeadRevision(FetchedRevision):
    link: PullRequestLink

    @property
    def reference(self) -> str:
        return f'refs/agent-comms/cited/{self.link.number}'

    @property
    def fetch_refspec(self) -> str:
        return f'+pull/{self.link.number}/head:{self.reference}'


class DeliveryMerge(DeclaredFamily, affix="DeliveryMerge"):
    """The integration observed when Core checked a commit delivery; never a claim of install."""

    def superseded_links(self, publications: tuple[MergedLink, ...]) -> tuple[MergedLink, ...]:
        """Merged publications stop proving a corrected criterion together."""
        return publications

    @property
    @abstractmethod
    def line(self) -> str: ...


@dataclass(frozen=True)
class MainDeliveryMerge(DeliveryMerge):
    @property
    def line(self) -> str:
        return f"merged to {WorkBranches.main}"


@dataclass(frozen=True)
class BranchDeliveryMerge(DeliveryMerge):
    branch: str
    commit: str

    @property
    def line(self) -> str:
        return f"merged to {self.branch} as {self.commit}"


@dataclass(frozen=True)
class UnmergedDeliveryMerge(DeliveryMerge):
    branches: tuple[str, ...]

    def superseded_links(self, publications: tuple[MergedLink, ...]) -> tuple[MergedLink, ...]:
        return ()

    @property
    def line(self) -> str:
        return f"not merged; linked branches: {', '.join(self.branches)}"


@dataclass(frozen=True)
class PullRequestLink:
    """The GitHub identity recorded by the order that delivers it."""
    repo: RepositoryName
    number: int

    def __post_init__(self) -> None:
        if FieldCodec.decode(int, self.number) <= 0:
            raise TodoError("A pull request number must be positive.")

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo.to_wire()}/pull/{self.number}"

    @staticmethod
    def _reported_number(repo: RepositoryName, reference: str) -> int:
        result = subprocess.run(
            ("gh", "pr", "view", reference, "-R", repo.to_wire(),
             "--json", "number", "--jq", ".number"),
            capture_output=True, text=True, timeout=60, check=True)
        return FieldCodec.decode(int, FieldCodec.parse(result.stdout))

    @classmethod
    def created(cls, repo: RepositoryName, reference: str) -> Self:
        """GitHub resolves its opaque creation result; we never parse its URL."""
        return cls(repo, cls._reported_number(repo, reference))

    @classmethod
    def existing(cls, repo: RepositoryName, number: int) -> Self:
        link = cls(repo, number)
        if cls._reported_number(repo, str(number)) != number:
            raise TodoError(f"GitHub did not report pull request {number} in {repo.to_wire()}.")
        return link


@dataclass(frozen=True)
class MergedLink:
    """A pull request an order links and the merge commit the order recorded for it."""
    link: PullRequestLink
    sha: str


def _github_time(text: str) -> float:
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


@dataclass(frozen=True)
class PullRequestCommit:
    """GitHub's mergeCommit object on a merged request."""
    oid: str


@dataclass(frozen=True)
class MergeBlock(DeclaredFamily, affix="MergeBlock"):
    """Why GitHub will not merge a head, decoded once from the forge row."""
    at: str

    @cached_property
    def since(self) -> float:
        return _github_time(self.at)

    @abstractmethod
    def what(self) -> str: ...

    @abstractmethod
    def remedy(self) -> str: ...

    def red_description(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class FailedCheckMergeBlock(MergeBlock):
    """A required check concluded red on the head."""
    name: str

    def red_description(self) -> tuple[str, ...]:
        return (self.what(),)

    def what(self) -> str:
        return f"required check {self.name} failed"

    def remedy(self) -> str:
        return f"fix {self.name} at its owner"


@dataclass(frozen=True)
class ConflictMergeBlock(MergeBlock):
    """The head conflicts with its base; no check can turn it green."""

    def what(self) -> str:
        return "conflicts with its base"

    def remedy(self) -> str:
        return "merge main into it"


@dataclass(frozen=True)
class PendingCheckMergeBlock(MergeBlock):
    name: str

    def what(self) -> str:
        return f'required check {self.name} has not finished'

    def remedy(self) -> str:
        return f'inspect {self.name} at its owner'


@dataclass(frozen=True)
class MissingReviewMergeBlock(MergeBlock):
    def what(self) -> str:
        return 'BLOCKED: no successful agent-comms/review on this head'

    def remedy(self) -> str:
        return 'judge HEAD'


@dataclass(frozen=True)
class RefusedCarryMergeBlock(MergeBlock):
    """The forge refused the carried review status on this head; only this head pays for it."""
    refusal: str

    def what(self) -> str:
        return f"carried review status refused: {self.refusal}"

    def remedy(self) -> str:
        return "restore the repository's write access or drop it from the teammate's repos"


@dataclass(frozen=True)
class BehindMergeBlock(MergeBlock):
    def what(self) -> str:
        return 'BEHIND: head is behind its base'

    def remedy(self) -> str:
        return 'queue'


@dataclass(frozen=True)
class WaitingMergeBlock(MergeBlock):
    """The grace period belongs to the cause, not to a second standing rule."""
    seconds: ClassVar[int] = 3 * 60

    @cached_property
    def since(self) -> float:
        return super().since + self.seconds

    def what(self) -> str:
        return 'required checks finished but the armed head has not merged'

    def remedy(self) -> str:
        return 'queue'


@dataclass(frozen=True)
class PullRequestUpdate:
    number: int
    updated: str


@dataclass(frozen=True)
class PullRequestUpdatePage:
    """Changed identities; the existing query decoder still owns PR state."""
    rows: tuple[PullRequestUpdate, ...]
    page_info: PageInfo
