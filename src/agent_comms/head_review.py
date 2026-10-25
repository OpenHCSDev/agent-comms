"""A reviewer's verdict on one exact pull-request head, posted as main's required status.

Every path that posts that status goes through here.
"""

from __future__ import annotations

import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated, ClassVar

from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .field_codec import FieldCodec, GitCommitText
from .repository_name import RepositoryName


@dataclass(frozen=True)
class PullRequestHead:
    """A pull request head as its reviewer read it; a verdict goes only on the current head."""

    repo: RepositoryName
    pr: int
    head: Annotated[str, GitCommitText]

    def __post_init__(self) -> None:
        if FieldCodec.decode(int, self.pr) <= 0:
            raise ValueError("A pull request number must be positive.")
        GitCommitText.decode(self.head)

    @classmethod
    def among(cls, repositories: tuple[RepositoryName, ...], pr: int, head: str) -> PullRequestHead:
        """The one candidate repository whose pull request ``pr`` is at ``head``. A single
        candidate is the answer; several are told apart by GitHub, and none or two holding
        that head is refused with what each one answered."""
        candidates = tuple(cls(repository, pr, head) for repository in repositories)
        if len(candidates) == 1:
            return candidates[0]
        reads = tuple((candidate, candidate.read()) for candidate in candidates)
        held = tuple(candidate for candidate, read in reads
                     if not read.returncode and read.stdout.strip().startswith(head))
        if len(held) != 1:
            answered = "; ".join(
                f"{candidate.repo.to_wire()}#{pr}: {(read.stdout.strip() or read.stderr.strip())[:60]}"
                for candidate, read in reads)
            raise ValueError(f"{len(held)} registered repositories have #{pr} at {head}; "
                             f"select --repo. {answered}")
        return held[0]

    def read(self) -> subprocess.CompletedProcess[str]:
        """GitHub's answer for this pull request's head commit."""
        return self.repo.rest(f"pulls/{self.pr}", "--jq", ".head.sha")

    def current(self) -> str:
        """The PR's full head commit, refused unless the reviewed head is still it."""
        read = self.read()
        if read.returncode:
            raise ToolRefusedError(read.stderr.strip() or f"#{self.pr} could not be read.")
        current = GitCommitText.decode(read.stdout.strip())
        if not current.startswith(self.head):
            raise ToolRefusedError(
                f"#{self.pr} moved to {current[:9]} since {self.head} was reviewed; review again."
            )
        return current


class HeadReview(DeclaredFamily, affix="HeadReview"):
    context: ClassVar[str] = "agent-comms/review"
    # GitHub's commit-status states, an external contract.
    state: ClassVar[str]
    # GitHub caps a status description at 140 characters.
    description_limit: ClassVar[int] = 140

    @abstractmethod
    def description(self, reviewer: str, subject: str) -> str: ...

    def post(self, repo: RepositoryName, head: str, reviewer: str, subject: str) -> None:
        posted = repo.rest(
            f"statuses/{head}",
            "--method",
            "POST",
            "-f",
            f"state={self.state}",
            "-f",
            f"context={self.context}",
            "-f",
            f"description={self.description(reviewer, subject)[:self.description_limit]}",
        )
        if posted.returncode:
            raise ToolRefusedError(posted.stderr.strip() or f"Review status on {head} failed.")
