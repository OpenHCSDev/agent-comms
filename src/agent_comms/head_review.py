"""A reviewer's verdict on one exact pull-request head, posted as main's required status.

Every path that posts that status goes through here.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, ClassVar
from collections.abc import Sequence

from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .field_codec import FieldCodec, GitCommitText, MalformedRecord, RecordProblems
from .repo import Repo
from .repository_name import RepositoryName
from .work_branches import WorkBranches

if TYPE_CHECKING:
    from .goal_management import Goals
    from .thread_identity import ThreadIncarnation
    from .todos import HeadReviewRecord, HeadVerdict, Todo


@dataclass(frozen=True, kw_only=True)
class HeadReviewStatus:
    """The forge's review context, shared by listed and linked PR views."""

    status_contexts: tuple[str, ...] = ()

    @property
    def has_head_review(self) -> bool:
        return HeadReview.context in self.status_contexts


@dataclass(frozen=True)
class PullRequestChange:
    """Git's whitespace-preserving identity of a pull request's complete patch."""

    patch: str


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
    def at(cls, repo: RepositoryName, pr: int) -> OpenPullRequestHead:
        """Read GitHub's head, its base branch and draft state in one request."""
        read = repo.rest(f"pulls/{pr}", "--jq",
                         '.head.sha + " " + .base.ref + " " + (.draft | tostring)')
        if read.returncode:
            raise ToolRefusedError(read.stderr.strip() or f"#{pr} could not be read.")
        head, base, draft = read.stdout.split()
        return OpenPullRequestHead.drafted(draft)(repo, pr, head, base=base)

    def change_identity(self, reader: WorkBranches, base: str) -> PullRequestChange:
        """Compare the PR's patch, never the unrelated changes in its base tree.

        Verbatim patch IDs preserve Python indentation and whitespace in literals.
        The existing Git reader shares one base-head read and fetches missing objects.
        """
        return PullRequestChange(reader.pull_request_patch_id(base, self.head))

    def holds(self, commit: str) -> bool:
        return self.head.startswith(commit)

    def same_pull_request(self, other: PullRequestHead) -> bool:
        """One pull request, whichever head each side read."""
        return self.repo == other.repo and self.pr == other.pr

    def current(self) -> OpenPullRequestHead:
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


class MergeArm(DeclaredFamily, affix="MergeArm"):
    """What a head verdict did to the PR's auto-merge, as the verb reports it."""

    @abstractmethod
    def lines(self, request: PullRequestHead) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class UntouchedMergeArm(MergeArm):
    """Only acceptance arms; every other verdict leaves auto-merge as it was."""

    def lines(self, request: PullRequestHead) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class DraftMergeArm(MergeArm):
    def lines(self, request: PullRequestHead) -> tuple[str, ...]:
        return (f"#{request.pr} is a draft, so not armed",)


@dataclass(frozen=True)
class RequestedMergeArm(MergeArm):
    head: Annotated[str, GitCommitText]

    def lines(self, request: PullRequestHead) -> tuple[str, ...]:
        return (f"#{request.pr} merge requested at {self.head}",)


@dataclass(frozen=True)
class OpenPullRequestHead(ReadPullRequestHead):
    """GitHub's read of an open PR and whether its declared state permits integration."""

    # GitHub's REST ``draft`` field as ``tostring`` renders it.
    forge_draft: ClassVar[str]

    @classmethod
    def drafted(cls, draft: str) -> type[OpenPullRequestHead]:
        return next(member for member in cls.__subclasses__() if member.forge_draft == draft)

    @abstractmethod
    def arm(self) -> MergeArm:
        """Request normal integration at this exact head; later pushes need a new review."""


@dataclass(frozen=True)
class DraftPullRequestHead(OpenPullRequestHead):
    forge_draft = "true"

    def arm(self) -> MergeArm:
        return DraftMergeArm()


@dataclass(frozen=True)
class ReadyPullRequestHead(OpenPullRequestHead):
    forge_draft = "false"

    def arm(self) -> MergeArm:
        requested = self.repo.command(
            "pr", "merge", str(self.pr), "--repo", self.repo.to_wire(),
            "--auto", "--merge", "--match-head-commit", self.head,
        )
        if requested.returncode:
            raise ToolRefusedError(
                f"#{self.pr} verdict recorded but merge was not requested: "
                f"{requested.stderr.strip() or 'gh pr merge failed'}")
        return RequestedMergeArm(self.head)


class HeadReviewRelation(DeclaredFamily):
    """Order relationships share one declaration family and compose their capabilities."""


class HeadReviewOrder(HeadReviewRelation, declared_name=None):
    """The input that authorizes a head review's optional order change."""

    def prepared(self, goals: Goals, problems: dict[str, str]) -> tuple[HeadReviewOrder, ...]:
        """A resolved relationship can authenticate; a refused selection contributes errors."""
        return (self,)

    @staticmethod
    def holding(todos: Sequence[Todo], request: PullRequestHead) -> tuple[Todo, ...]:
        """The open orders whose current declaration holds this head, held or released."""
        return tuple(todo for todo in todos if todo.repo == request.repo
                     and todo.order.holds_head(request))

    @abstractmethod
    def require_reviewer(self, goals: Goals, request: PullRequestHead,
                         actor: ThreadIncarnation) -> None: ...

    def director_identity(self, goals: Goals, actor: ThreadIncarnation) -> ThreadIncarnation:
        return actor

    @abstractmethod
    def record_review(self, goals: Goals, request: PullRequestHead, verdict: HeadVerdict,
                      actor: ThreadIncarnation) -> HeadReviewRecord: ...


@dataclass(frozen=True)
class PreparedHeadReview:
    """The current forge head and its checked status text, read once before recording."""

    current: OpenPullRequestHead
    subject: str


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
