"""A reviewer's verdict on one exact pull-request head, posted as the review status the merge
queue requires (agent-comms/review). Every path that posts that status goes through here."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .repository_name import RepositoryName


@dataclass(frozen=True)
class PullRequestHead:
    """A pull request head as its reviewer read it; a verdict goes only on the current head."""

    repo: RepositoryName
    pr: int
    head: str

    def current(self) -> str:
        """The PR's full head commit, refused unless the reviewed head is still it."""
        read = self.repo.rest(f"pulls/{self.pr}", "--jq", ".head.sha")
        if read.returncode:
            raise ToolRefusedError(read.stderr.strip() or f"#{self.pr} could not be read.")
        current = read.stdout.strip()
        if not current.startswith(self.head):
            raise ToolRefusedError(
                f"#{self.pr} moved to {current[:9]} since {self.head} was reviewed; review again."
            )
        return current


class HeadReview(DeclaredFamily, affix="HeadReview"):
    context: ClassVar[str] = "agent-comms/review"
    # The merge-queue job (debt-ratchet.yml) that reads ``context`` on the queued head.
    queue_check: ClassVar[str] = "review-verdict"
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


@dataclass(frozen=True)
class AcceptedHeadReview(HeadReview):
    state = "success"

    def description(self, reviewer: str, subject: str) -> str:
        return f"Accepted by {reviewer} on {subject}"


@dataclass(frozen=True)
class SentBackHeadReview(HeadReview):
    """The head goes back; the reason names the defect, the PR's review comment the detail."""

    reason: str
    state = "failure"

    def __post_init__(self) -> None:
        if len(self.reason.split()) < 4:
            raise ValueError("Say what is wrong with the head in 4+ words.")

    def description(self, reviewer: str, subject: str) -> str:
        return f"Sent back by {reviewer} on {subject}: {self.reason}"
