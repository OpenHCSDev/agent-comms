"""A reviewer's verdict on one exact pull-request head, posted as main's required status.

Every path that posts that status goes through here.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .field_codec import FieldCodec, GitCommitText, MalformedRecord, RecordProblems
from .repo import Repo
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
        that head refuses repository selection, including any failed GitHub reads."""
        candidates = tuple(cls(repository, pr, head) for repository in repositories)
        if len(candidates) == 1:
            return candidates[0]
        observed = []
        problems: dict[str, str] = {}
        for candidate in candidates:
            try:
                observed.append(cls.at(candidate.repo, pr))
            except ToolRefusedError as refused:
                problems |= MalformedRecord(str(refused)).located(
                    f'--repo {candidate.repo.to_wire()}', 'check access to this repository and pull request')
        held = tuple(candidate for candidate in observed if candidate.holds(head))
        if len(held) != 1:
            problems |= MalformedRecord(Repo.selection_refusal()).located(
                '--repo', 'select one registered repository with --repo')
            raise RecordProblems(problems)
        return cls(held[0].repo, pr, head)

    @classmethod
    def at(cls, repo: RepositoryName, pr: int) -> ReadPullRequestHead:
        """Read GitHub's head and the branch it merges into, in one request."""
        read = repo.rest(f"pulls/{pr}", "--jq", '.head.sha + " " + .base.ref')
        if read.returncode:
            raise ToolRefusedError(read.stderr.strip() or f"#{pr} could not be read.")
        head, base = read.stdout.split()
        return ReadPullRequestHead(repo, pr, head, base=base)

    def holds(self, commit: str) -> bool:
        return self.head.startswith(commit)

    def same_pull_request(self, other: PullRequestHead) -> bool:
        """One pull request, whichever head each side read."""
        return self.repo == other.repo and self.pr == other.pr

    def current(self) -> ReadPullRequestHead:
        """GitHub's read of the PR, refused unless the reviewed head is still its head."""
        current = self.at(self.repo, self.pr)
        if not current.holds(self.head):
            raise ToolRefusedError(
                f"#{self.pr} moved to {current.head[:9]} since {self.head} was reviewed; review again."
            )
        return current


@dataclass(frozen=True)
class ReadPullRequestHead(PullRequestHead):
    """The head as GitHub reported it, with the base branch the same read named."""

    base: str = field(kw_only=True)


class HeadReview(DeclaredFamily, affix="HeadReview"):
    context: ClassVar[str] = "agent-comms/review"
    # GitHub's commit-status states, an external contract.
    state: ClassVar[str]
    # GitHub caps a status description at 140 characters.
    description_limit: ClassVar[int] = 140

    @classmethod
    def allows_integration(cls) -> bool:
        return False

    @classmethod
    def accepted(cls, repo: RepositoryName, head: str) -> bool:
        """Read the latest status in our declared context on this exact commit."""
        read = repo.rest(
            f"commits/{head}/status", "--jq",
            f"[.statuses[] | select(.context == {json.dumps(cls.context)}) | .state]",
        )
        if read.returncode:
            raise ToolRefusedError(read.stderr.strip() or f"Review status on {head} failed.")
        states = FieldCodec.decode(tuple[str, ...], FieldCodec.parse(read.stdout))
        return any(member.state in states and member.allows_integration()
                   for member in cls.members_with(cls))

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
