"""A reviewer's verdict on one exact pull-request head, posted as main's required status.

Every path that posts that status goes through here.
"""

from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar
from collections.abc import Sequence

from .build_identity import RepositoryCommit
from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .field_codec import FieldCodec, GitCommitText, MalformedRecord, RecordProblems
from .installer.runtime_resolution import StackSources
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
    integration_digest: str = ""


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
                         '.head.sha + " " + .base.ref + " " + (.draft | tostring) + " " + .state')
        if read.returncode:
            raise ToolRefusedError(read.stderr.strip() or f"#{pr} could not be read.")
        head, base, draft, state = read.stdout.split()
        if state != "open":
            raise ToolRefusedError(f"#{pr} is no longer an open source.")
        return OpenPullRequestHead.drafted(draft)(repo, pr, head, base=base)

    def change_identity(self, reader: WorkBranches, base: str) -> PullRequestChange:
        """Compare the PR's patch, never the unrelated changes in its base tree.

        Verbatim patch IDs preserve Python indentation and whitespace in literals.
        The existing Git reader shares one base-head read and fetches missing objects.
        """
        return PullRequestChange(
            reader.pull_request_patch_id(base, self.head),
            IntegrationInput.digest_from_message(reader.commit_message(self.head)),
        )

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
class IntegrationSource:
    """One repository's captured main, exact PR overlays and resulting Git tree."""

    baseline: RepositoryCommit
    heads: tuple[PullRequestHead, ...]
    candidate_tree: Annotated[str, GitCommitText]

    def __post_init__(self) -> None:
        for commit in (self.baseline.commit, self.candidate_tree, *(head.head for head in self.heads)):
            GitCommitText.decode(commit)
            if len(commit) != 40 or commit != commit.lower():
                raise MalformedRecord("Integration inputs require full lowercase 40-hex Git identities.")
        if any(head.repo != self.repository for head in self.heads):
            raise MalformedRecord("Every integration member must belong to its source repository.")
        if len({head.pr for head in self.heads}) != len(self.heads):
            raise MalformedRecord("An integration source cannot name a PR twice.")
        if self.heads != tuple(sorted(self.heads, key=lambda head: head.pr)):
            raise MalformedRecord("Integration members must be in PR-number order.")

    @property
    def repository(self) -> RepositoryName:
        return self.baseline.repository

    @staticmethod
    def main_head(repository: Repo) -> str:
        rows = Repo.git(repository.checkout, "ls-remote", "--exit-code", "origin", "refs/heads/main").splitlines()
        if len(rows) != 1:
            raise ToolRefusedError(f"No unique main in {repository.github.to_wire()}")
        commit, reference = rows[0].split()
        if reference != "refs/heads/main":
            raise ToolRefusedError("The remote answered with a different main reference")
        GitCommitText.decode(commit)
        if len(commit) != 40:
            raise ToolRefusedError("Remote main did not resolve to a full Git identity")
        return commit

    def expected_tree(self, repository: Repo) -> str:
        """Reproduce the ordered Git merge result without moving a registered checkout."""
        reader = WorkBranches(repository.checkout)
        reader.require_commits("origin", self.baseline.commit, *(head.head for head in self.heads))
        commit = self.baseline.commit
        for head in self.heads:
            tree = Repo.git(repository.checkout, "merge-tree", "--write-tree", commit, head.head).strip()
            commit = Repo.git(repository.checkout, "commit-tree", tree,
                              "-p", commit, "-p", head.head,
                              "-m", "Validate captured source combination").strip()
        return Repo.git(repository.checkout, "rev-parse", f"{commit}^{{tree}}").strip()

    def validate(self, repository: Repo, carrier: PullRequestHead) -> None:
        if Repo.of(repository.checkout).github != self.repository:
            raise ToolRefusedError("A registered source checkout has a different Git origin")
        if self.main_head(repository) != self.baseline.commit:
            raise ToolRefusedError(f"{self.repository.to_wire()} main moved; recapture inputs")
        if self.expected_tree(repository) != self.candidate_tree:
            raise ToolRefusedError(f"{self.repository.to_wire()} candidate tree is not its captured combination")
        for head in self.heads:
            current = PullRequestHead.at(head.repo, head.pr)
            if current.base != "main":
                raise ToolRefusedError(f"#{head.pr} no longer targets captured main")
            expected = carrier.head if head.same_pull_request(carrier) else head.head
            if current.head != expected:
                raise ToolRefusedError(f"{head.repo.to_wire()}#{head.pr} moved; recapture inputs")


@dataclass(frozen=True)
class IntegrationInput:
    """Immutable compared source cuts, bound by their carrier's reviewed Git commit.

    This is not an authorization record. Exact-head review and draft state still decide
    promotion. The canonical payload deliberately cannot contain its own carrier SHA.
    """

    sources: tuple[IntegrationSource, ...]
    trailer_name: ClassVar[str] = "Agent-Comms-Integration"

    def __post_init__(self) -> None:
        repositories = tuple(source.repository for source in self.sources)
        if not repositories or len(set(repositories)) != len(repositories):
            raise MalformedRecord("Integration inputs require unique, nonempty repository sources.")
        if repositories != tuple(sorted(repositories, key=lambda repo: repo.key)):
            raise MalformedRecord("Integration sources must be in repository-name order.")

    def require_repositories(self, repositories: tuple[RepositoryName, ...]) -> None:
        """The actual stack declaration supplies coverage, never a caller's package roster."""
        if {source.repository for source in self.sources} != set(repositories):
            raise ToolRefusedError("Integration inputs omit a declared stack source or name an unknown one.")

    def source(self, repository: RepositoryName) -> IntegrationSource:
        selected = tuple(source for source in self.sources if source.repository == repository)
        if len(selected) != 1:
            raise ToolRefusedError(f"Integration inputs do not uniquely select {repository.to_wire()}.")
        return selected[0]

    @staticmethod
    def declared_repositories(core: Repo, baseline: str, candidate: str) -> tuple[RepositoryName, ...]:
        """Actual declarations on both sides own the universe, including added/removed clients."""
        declarations = tuple(StackSources.declared_sources(
            Repo.git(core.checkout, "show", f"{commit}:stack/pyproject.toml"))
            for commit in (baseline, candidate))
        return tuple(sorted({core.github, *(Repo.repository_url(url)
            for side in declarations for url, branch in side.values())}, key=lambda name: name.key))

    def validate(self, repositories: tuple[Repo, ...], carrier: PullRequestHead) -> None:
        """Git and forge prove this frozen input; Todo and verdict owners decide permission."""
        core = Repo.selected(repositories, carrier.repo.to_wire())
        WorkBranches(core.checkout).require_commits("origin", carrier.head)
        source = self.source(carrier.repo)
        self.require_repositories(self.declared_repositories(core, source.baseline.commit, carrier.head))
        members = tuple(head for head in source.heads if head.same_pull_request(carrier))
        if len(members) != 1:
            raise ToolRefusedError("The integration carrier is not a unique captured local PR")
        if members[0].head == carrier.head:
            raise ToolRefusedError("An input captures the carrier's earlier source, never its own commit SHA")
        for entry in self.sources:
            entry.validate(Repo.selected(repositories, entry.repository.to_wire()), carrier)
        for head in source.heads:
            if not Repo.descends(core.checkout, head.head, carrier.head):
                raise ToolRefusedError(f"The carrier dropped original source #{head.pr}")
        if Repo.git(core.checkout, "rev-parse", f"{carrier.head}^{{tree}}").strip() != source.candidate_tree:
            raise ToolRefusedError("The carrier tree differs from its captured candidate")
        if self.from_message(WorkBranches(core.checkout).commit_message(carrier.head)) != (self,):
            raise ToolRefusedError("The current carrier does not contain this exact canonical input")
        StackSources.require_compatible(
            Repo.git(core.checkout, "show", f"{source.baseline.commit}:stack/pyproject.toml"),
            Repo.git(core.checkout, "show", f"{carrier.head}:stack/pyproject.toml"))

    @property
    def canonical(self) -> str:
        return FieldCodec.compact(FieldCodec.encode(self), sort_keys=True, allow_nan=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    @property
    def trailer(self) -> str:
        return f"{self.trailer_name}: {self.canonical}"

    @classmethod
    def from_message(cls, message: str) -> tuple[IntegrationInput, ...]:
        """Absent is ordinary; any attempted malformed/duplicate trailer refuses."""
        lines = message.rstrip("\n").split("\n")
        marked = tuple(line for line in lines if line.lstrip().startswith(cls.trailer_name))
        if not marked:
            return ()
        prefix = f"{cls.trailer_name}: "
        if len(marked) != 1 or not marked[0].startswith(prefix) or marked[0] != lines[-1]:
            raise MalformedRecord("Integration metadata requires one final canonical trailer.")
        payload = marked[0].removeprefix(prefix)
        decoded = FieldCodec.decode(cls, FieldCodec.parse(payload))
        if decoded.canonical != payload:
            raise MalformedRecord("Integration metadata is not canonical.")
        return (decoded,)

    @classmethod
    def digest_from_message(cls, message: str) -> str:
        return "".join(inputs.digest for inputs in cls.from_message(message))


class Side(DeclaredFamily, affix="Side"):
    """One compared source cut; selection and materialized path belong to this member."""

    @staticmethod
    @abstractmethod
    def ref(base: str, head: str) -> str:
        """This side's revision of the compared pair."""

    @classmethod
    def sides(cls) -> tuple[type[Side], ...]:
        return cls.members_with(cls)

    @classmethod
    def checkout(cls, directory: Path, repository: RepositoryName) -> Path:
        return directory / cls.declared_name / Path(*repository.key)


class BaseSide(Side):
    @staticmethod
    def ref(base: str, head: str) -> str:
        return base


class HeadSide(Side):
    @staticmethod
    def ref(base: str, head: str) -> str:
        return head


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
