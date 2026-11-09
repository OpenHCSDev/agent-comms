"""A reviewer's verdict on one exact pull-request head, posted as main's required status.

Every path that posts that status goes through here.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.installer.build_identity import RepositoryCommit
from agent_comms.installer.runtime_resolution import PrivateUvProject, StackSources
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GitCommitText,
    MalformedRecord,
    RecordProblems,
    boundary_decoder,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms_hooks import ToolRefusedError

if TYPE_CHECKING:
    from agent_comms.planning.goal_management import Goals
    from agent_comms.planning.orders import HeadReviewRecord, HeadVerdict, Todo
    from agent_comms.team.order import TeamContext


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

    @staticmethod
    def forge_read(repo: RepositoryName, pr: int) -> str:
        """GitHub's head, base branch, draft state and state in one request."""
        read = repo.rest(
            f"pulls/{pr}",
            "--jq",
            '.head.sha + " " + .base.ref + " " + (.draft | tostring) + " " + .state',
        )
        if read.returncode:
            raise ToolRefusedError(read.stderr.strip() or f"#{pr} could not be read.")
        return read.stdout

    @classmethod
    def at(cls, repo: RepositoryName, pr: int) -> OpenPullRequestHead:
        return OpenPullRequestHead.from_forge(repo, pr, cls.forge_read(repo, pr))

    def change_identity(self, reader: WorkBranches, base: str) -> PullRequestChange:
        """Compare the PR's patch, never the unrelated changes in its base tree.

        Verbatim patch IDs preserve Python indentation and whitespace in literals.
        The existing Git reader shares one base-head read and fetches missing objects.
        """
        return PullRequestChange(
            reader.pull_request_patch_id(base, self.head),
            IntegrationInput.digest_from_message(reader.commit_message(self.head)),
        )

    @property
    def identity(self) -> PullRequestHead:
        """Capture repository/PR/commit, not the forge observation's transient draft state."""
        return PullRequestHead(self.repo, self.pr, self.head)

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
                f"#{self.pr} moved to {current.head[:9]} since {self.head} was reviewed; "
                "review again."
            )
        return current

    def published_after(self, previous: PullRequestHead) -> OpenPullRequestHead:
        """Observe a known successful push; only its previous head may lag briefly."""
        if not self.same_pull_request(previous):
            raise ToolRefusedError("Publication observations must name the same pull request")
        deadline = monotonic() + 5
        current = self.at(self.repo, self.pr)
        while not current.holds(self.head):
            if not current.holds(previous.head) or monotonic() >= deadline:
                raise ToolRefusedError(
                    f"Published #{self.pr} expected {self.head}; forge observed {current.head}; "
                    f"previous head {previous.head}. Preserve this attempt and resume it; "
                    "do not repeat creation."
                )
            sleep(min(0.1, max(0, deadline - monotonic())))
            current = self.at(self.repo, self.pr)
        return current

    def closed_or_open(self) -> ReadPullRequestHead:
        """GitHub's read in whatever state the PR is in, refused unless this is still its head:
        a closed PR's head can only be dropped, so nothing here may arm or integrate it."""
        head, base, *_ = self.forge_read(self.repo, self.pr).split()
        current = ReadPullRequestHead(self.repo, self.pr, head, base=base)
        if not current.holds(self.head):
            raise ToolRefusedError(
                f"#{self.pr} moved to {current.head[:9]} since {self.head} was reviewed; review again."
            )
        return current


class SourceCheckout(Checkout, ABC):
    """The nominal checkout capability a captured source needs to build its tree."""

    __slots__ = ()

    @abstractmethod
    def combine_sources(self, base: str, heads: tuple[PullRequestHead, ...]) -> str: ...

    @classmethod
    @abstractmethod
    def create(cls, repository: Repo, path: Path, commit: str) -> SourceCheckout: ...

    @classmethod
    @abstractmethod
    def materialize(
        cls, source: IntegrationSource, repository: Repo, path: Path
    ) -> SourceCheckout: ...


class InputPublication(ABC):
    """The publication operations a frozen source input requires of its executor."""

    checkout: SourceCheckout

    @abstractmethod
    def capture(
        self, context: TeamContext, prepared: UnpublishedInput, carrier: PullRequestHead
    ) -> CombinedInput: ...

    @abstractmethod
    def retain_capture(self, inputs: CombinedInput, commit: str) -> None: ...


@dataclass(frozen=True)
class IntegrationSource(DeclaredFamily, affix="Source"):
    """One repository's captured main, exact PR overlays and resulting Git tree."""

    baseline: RepositoryCommit
    heads: tuple[PullRequestHead, ...]
    candidate_tree: Annotated[str, GitCommitText]

    def __post_init__(self) -> None:
        for commit in (
            self.baseline.commit,
            self.candidate_tree,
            *(head.head for head in self.heads),
        ):
            GitCommitText.decode(commit)
            if len(commit) != 40 or commit != commit.lower():
                raise MalformedRecord(
                    "Integration inputs require full lowercase 40-hex Git identities."
                )
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
        reference_name = f"refs/heads/{WorkBranches.main}"
        rows = Repo.git(
            repository.checkout, "ls-remote", "--exit-code", "origin", reference_name
        ).splitlines()
        if len(rows) != 1:
            raise ToolRefusedError(f"No unique main in {repository.github.to_wire()}")
        commit, reference = rows[0].split()
        if reference != reference_name:
            raise ToolRefusedError("The remote answered with a different main reference")
        GitCommitText.decode(commit)
        if len(commit) != 40:
            raise ToolRefusedError("Remote main did not resolve to a full Git identity")
        return commit

    def has_changes(self, repository: Repo) -> bool:
        """Compare this captured tree with its own repository's baseline, not Core's."""
        self.require_origin(repository)
        baseline_tree = Repo.git(
            repository.checkout, "rev-parse", f"{self.baseline.commit}^{{tree}}"
        ).strip()
        return self.candidate_tree != baseline_tree

    @property
    def local_sources(self) -> tuple[LocalIntegrationSource, ...]:
        return ()

    @property
    def companion_sources(self) -> tuple[IntegrationSource, ...]:
        return (self,)

    def require_ordinary(self, carrier: PullRequestHead) -> None:
        if self.heads:
            raise ToolRefusedError("An ordinary comparison cannot supply companion overlays")

    def materialize_head(
        self, repository: Repo, path: Path, tested: str, checkout: type[SourceCheckout]
    ) -> SourceCheckout:
        return checkout.materialize(self, repository, path)

    @property
    def candidate_commit(self) -> str:
        return self.baseline.commit

    def prepare_candidate(self, checkout: SourceCheckout) -> None:
        checkout.combine_sources(self.baseline.commit, self.heads)

    def require_origin(self, repository: Repo) -> None:
        if Repo.of(repository.checkout).github != self.repository:
            raise ToolRefusedError("A source checkout has a different Git origin")

    def validate(self, repository: Repo, carrier: PullRequestHead) -> None:
        self.require_origin(repository)
        if self.main_head(repository) != self.baseline.commit:
            raise ToolRefusedError(f"{self.repository.to_wire()} main moved; recapture inputs")
        self.require_candidate(repository, carrier)

    @abstractmethod
    def require_tree(self, repository: Repo) -> None:
        """Prove this source's candidate through its declared Git construction."""

    def require_ancestry(self, repository: Repo, candidate: str) -> None:
        """A resolved Git combination retains every actual source, not a guessed patch."""
        self.require_origin(repository)
        repository.acquire(self.baseline.commit, candidate, *(head.head for head in self.heads))
        if not Repo.descends(repository.checkout, self.baseline.commit, candidate) or any(
            not Repo.descends(repository.checkout, head.head, candidate) for head in self.heads
        ):
            raise ToolRefusedError("The resolved candidate dropped a captured source parent")
        if (
            Repo.git(repository.checkout, "rev-parse", f"{candidate}^{{tree}}").strip()
            != self.candidate_tree
        ):
            raise ToolRefusedError("The resolved candidate tree differs from its captured source")

    def require_candidate(self, repository: Repo, carrier: PullRequestHead) -> None:
        """An ordinary/companion combination is reproduced; every source head stays exact."""
        self.require_tree(repository)
        self.require_heads(carrier)

    def require_heads(self, carrier: PullRequestHead) -> None:
        for head in self.heads:
            if not head.same_pull_request(carrier):
                current = head.current()
                if current.base != WorkBranches.main:
                    raise ToolRefusedError(f"#{head.pr} no longer targets captured main")


@dataclass(frozen=True)
class MergedIntegrationSource(IntegrationSource):
    """Ordered source heads whose reproducible Git merge owns the candidate tree."""

    def expected_tree(self, repository: Repo) -> str:
        repository.acquire(self.baseline.commit, *(head.head for head in self.heads))
        commit = self.baseline.commit
        for head in self.heads:
            tree = Repo.git(
                repository.checkout, "merge-tree", "--write-tree", commit, head.head
            ).strip()
            commit = Repo.git(
                repository.checkout,
                "commit-tree",
                tree,
                "-p",
                commit,
                "-p",
                head.head,
                "-m",
                "Validate captured source combination",
            ).strip()
        return Repo.git(repository.checkout, "rev-parse", f"{commit}^{{tree}}").strip()

    def require_tree(self, repository: Repo) -> None:
        self.require_origin(repository)
        if self.expected_tree(repository) != self.candidate_tree:
            raise ToolRefusedError(
                f"{self.repository.to_wire()} candidate tree is not its captured combination"
            )


@dataclass(frozen=True)
class LocalIntegrationSource(MergedIntegrationSource):
    """The tested repository's actual merge, not a replay of its own published PR."""

    @property
    def local_sources(self) -> tuple[LocalIntegrationSource, ...]:
        return (self,)

    @property
    def companion_sources(self) -> tuple[IntegrationSource, ...]:
        return ()

    def require_ordinary(self, carrier: PullRequestHead) -> None:
        if self.heads != (carrier,):
            raise ToolRefusedError("An ordinary comparison requires its exact local carrier")

    def materialize_head(
        self, repository: Repo, path: Path, tested: str, checkout: type[SourceCheckout]
    ) -> SourceCheckout:
        return checkout.create(repository, path, tested)


@dataclass(frozen=True)
class ResolvedIntegrationSource(IntegrationSource):
    """A separate published companion carrier preserves the actual resolved member tree."""

    carrier: PullRequestHead

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            self.carrier.repo != self.repository
            or len(self.carrier.head) != 40
            or self.carrier.head != self.carrier.head.lower()
        ):
            raise MalformedRecord("A resolved source requires its exact full carrier identity")
        if any(head.same_pull_request(self.carrier) for head in self.heads):
            raise MalformedRecord(
                "A resolved companion carrier is separate from its source members"
            )

    @property
    def candidate_commit(self) -> str:
        return self.carrier.head

    def prepare_candidate(self, checkout: SourceCheckout) -> None:
        self.require_ancestry(Repo.of(checkout.path), self.carrier.head)

    def require_tree(self, repository: Repo) -> None:
        self.require_ancestry(repository, self.carrier.head)
        current = self.carrier.current()
        if current.base != WorkBranches.main:
            raise ToolRefusedError("The resolved companion carrier no longer targets main")


@dataclass(frozen=True)
class IntegrationInput(DeclaredFamily, affix="Input"):
    """Immutable compared source cuts, bound by their carrier's reviewed Git commit.

    This is not an authorization record. Exact-head review and draft state still decide
    promotion. The canonical payload deliberately cannot contain its own carrier SHA.
    """

    sources: tuple[IntegrationSource, ...]
    trailer_name: ClassVar[str] = "Agent-Comms-Integration"
    filename: ClassVar[str] = "input.json"

    def __post_init__(self) -> None:
        repositories = tuple(source.repository for source in self.sources)
        if not repositories or len(set(repositories)) != len(repositories):
            raise MalformedRecord("Integration inputs require unique, nonempty repository sources.")
        if repositories != tuple(sorted(repositories, key=lambda repo: repo.key)):
            raise MalformedRecord("Integration sources must be in repository-name order.")
        _ = self.local

    @property
    def local(self) -> LocalIntegrationSource:
        selected = tuple(local for source in self.sources for local in source.local_sources)
        if len(selected) != 1:
            raise MalformedRecord("Integration inputs require exactly one local source.")
        (local,) = selected
        return local

    @property
    def companions(self) -> tuple[IntegrationSource, ...]:
        return tuple(companion for source in self.sources for companion in source.companion_sources)

    def require_repositories(self, repositories: tuple[RepositoryName, ...]) -> None:
        """The actual stack declaration supplies coverage, never a caller's package roster."""
        if {source.repository for source in self.sources} != set(repositories):
            raise ToolRefusedError(
                "Integration inputs omit a declared stack source or name an unknown one."
            )

    def source(self, repository: RepositoryName) -> IntegrationSource:
        selected = tuple(source for source in self.sources if source.repository == repository)
        if len(selected) != 1:
            raise ToolRefusedError(
                f"Integration inputs do not uniquely select {repository.to_wire()}."
            )
        return selected[0]

    @staticmethod
    def declared_repositories(
        core: Repo, baseline: str, candidate: str
    ) -> tuple[RepositoryName, ...]:
        """Actual declarations on both sides own the universe, including added/removed clients."""
        return tuple(
            sorted(
                {core.github, *IntegrationInput.source_urls(core, baseline, candidate)},
                key=lambda name: name.key,
            )
        )

    @staticmethod
    def source_urls(core: Repo, baseline: str, candidate: str) -> dict[RepositoryName, str]:
        urls: dict[RepositoryName, str] = {}
        for commit in (baseline, candidate):
            declared = StackSources.declared_sources(
                PrivateUvProject.decode(
                    Repo.git(core.checkout, "show", f"{commit}:stack/pyproject.toml")
                )
            )
            identities = tuple(Repo.repository_url(url) for url, branch in declared.values())
            if len(set(identities)) != len(identities):
                raise ToolRefusedError("One stack cut declares a Git repository more than once")
            for url, _branch in declared.values():
                identity = Repo.repository_url(url)
                if identity == core.github:
                    raise ToolRefusedError("The stack repeats Core as a Git companion source")
                urls[identity] = url
        return urls

    def validate(self, repositories: tuple[Repo, ...], carrier: PullRequestHead) -> None:
        """Git and forge prove this frozen input; Todo and verdict owners decide permission."""
        self._validate_current(repositories, carrier, carrier.current())

    def _validate_current(
        self, repositories: tuple[Repo, ...], carrier: PullRequestHead,
        current: OpenPullRequestHead,
    ) -> None:
        """One observed forge cut and the expected immutable capture answer different questions."""
        core = Repo.selected(repositories, carrier.repo.to_wire())
        if current.base != WorkBranches.main:
            raise ToolRefusedError("The integration carrier no longer targets main")
        self.require_carrier(core, carrier)
        self.require_local_candidate(core, carrier)
        local = self.local
        local.require_origin(core)
        if IntegrationSource.main_head(core) != local.baseline.commit:
            raise ToolRefusedError(f"{core.github.to_wire()} main moved; recapture inputs")
        for entry in self.companions:
            entry.validate(Repo.selected(repositories, entry.repository.to_wire()), carrier)
        self.require_substantive(repositories)

    def require_substantive(self, repositories: tuple[Repo, ...]) -> None:
        """Ordinary source comparisons do not require a combined change."""

    def resume_publication(
        self, run: InputPublication, context: TeamContext, carrier: PullRequestHead
    ) -> CombinedInput:
        """An ordinary source record cannot become a combined publication."""
        raise ToolRefusedError("This input is not a resumable combined publication")

    def require_recapture(self, core: Repo, carrier: PullRequestHead) -> None:
        """A completed capture supplies the default recapture proof."""
        self.require_carrier(core, carrier)

    def require_source_reviews(self) -> None:
        """Native promotion and the hosted gate require these same exact source decisions."""
        for source in self.sources:
            for head in source.heads:
                if not HeadReview.accepted(head.repo, head.head):
                    raise ToolRefusedError(
                        f"{head.repo.to_wire()}#{head.pr} at {head.head} has no accepted exact-head "
                        "source review; pending qualification cannot promote the carrier."
                    )

    @property
    def combined(self) -> bool:
        return False

    def carriers(self) -> tuple[IntegrationInput, ...]:
        """The inputs that owe a carrier's member proof; an ordinary source owes its head's ok."""
        return ()

    def pinned_sources(self, repository: RepositoryName) -> tuple[IntegrationSource, ...]:
        """The sources whose main must still be current: an ordinary PR tests its own repository."""
        return (self.source(repository),)

    def write_workflow_output(self, path: Path, repository: RepositoryName) -> None:
        """GitHub consumes these derived values, not another payload or branch-name rule."""
        with path.open("a") as output:
            output.write(f"base={self.source(repository).baseline.commit}\n")
            output.write(f"combined={str(self.combined).lower()}\n")
            output.write(f"tested={self.tested}\n")

    def require_carrier(self, core: Repo, carrier: PullRequestHead) -> None:
        raise ToolRefusedError("An individual source is not a captured combined carrier")

    def require_local_candidate(self, core: Repo, carrier: PullRequestHead) -> None:
        self.local.require_origin(core)
        self.local.require_candidate(core, carrier)

    @property
    def canonical(self) -> str:
        return FieldCodec.compact(FieldCodec.encode(self), sort_keys=True, allow_nan=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    @property
    def tested(self) -> str:
        """What a run over these inputs measures: each repository's baseline commit and the tree
        it tests. A pull request's merge commit and its merge-queue commit are different commits
        whose sources agree here, so a queue entry with the same tested sources as a green
        pull-request run has nothing new to check."""
        cut = [[source.repository.to_wire(), source.baseline.commit, source.candidate_tree]
               for source in self.sources]
        return hashlib.sha256(FieldCodec.compact(cut).encode("utf-8")).hexdigest()

    @property
    def trailer(self) -> str:
        return f"{self.trailer_name}: {self.canonical}"

    @classmethod
    def from_message(cls, message: str) -> tuple[Self, ...]:
        """Absent is ordinary; any attempted malformed/duplicate trailer refuses."""
        lines = message.rstrip("\n").split("\n")
        marked = tuple(line for line in lines if line.lstrip().startswith(cls.trailer_name))
        if not marked:
            return ()
        prefix = f"{cls.trailer_name}: "
        if len(marked) != 1 or not marked[0].startswith(prefix) or marked[0] != lines[-1]:
            raise MalformedRecord("Integration metadata requires one final canonical trailer.")
        payload = marked[0].removeprefix(prefix)
        return (cls.from_canonical(payload),)

    @classmethod
    def from_canonical(cls, payload: str) -> Self:
        decoded = FieldCodec.decode(cls, FieldCodec.parse(payload))
        if decoded.canonical != payload:
            raise MalformedRecord("Integration metadata is not canonical.")
        return decoded

    @classmethod
    def at(cls, directory: Path) -> Self:
        """A derived execution artifact; Git/event validation remains mandatory in CI."""
        return cls.from_canonical((directory / cls.filename).read_text().removesuffix("\n"))

    def write(self, directory: Path) -> None:
        (directory / self.filename).write_text(self.canonical + "\n")

    @classmethod
    def digest_from_message(cls, message: str) -> str:
        return "".join(inputs.digest for inputs in cls.from_message(message))


@dataclass(frozen=True)
class OrdinaryInput(IntegrationInput):
    """One source merges on its own head's recorded ok; it never promotes a combined carrier."""


@dataclass(frozen=True)
class SubstantiveInput(IntegrationInput, declared_name=None):
    """A source cut must change a declared source, before or after publication."""

    def require_substantive(self, repositories: tuple[Repo, ...]) -> None:
        """Any captured repository may supply the change; none may fabricate one."""
        if not any(
            source.has_changes(Repo.selected(repositories, source.repository.to_wire()))
            for source in self.sources
        ):
            raise ToolRefusedError("The captured sources have no substantive change")


@dataclass(frozen=True)
class UnpublishedInput(SubstantiveInput):
    """A real source cut whose carrier has not been published, never merge permission."""

    predecessor: RepositoryCommit
    trailer_name: ClassVar[str] = "Agent-Comms-Unpublished"
    filename: ClassVar[str] = "unpublished-input.json"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.predecessor.repository != self.local.repository:
            raise MalformedRecord("The prepared predecessor must belong to the local source")

    @classmethod
    @boundary_decoder
    def restore(cls, message: str, fresh: UnpublishedInput) -> UnpublishedInput:
        """An existing prospective message fixes the cut; absent metadata prepares anew."""
        recorded = cls.from_message(message)
        if not recorded:
            return fresh
        (frozen,) = recorded
        if frozen.sources != fresh.sources:
            raise ToolRefusedError("The prepared source cut changed; preserve and recapture explicitly")
        return frozen

    def require_current(self, repositories: tuple[Repo, ...]) -> None:
        """Recheck this same source cut, including during a resumed publication."""
        for source in self.companions:
            source.require_tree(Repo.selected(repositories, source.repository.to_wire()))
        for source in self.sources:
            repository = Repo.selected(repositories, source.repository.to_wire())
            if IntegrationSource.main_head(repository) != source.baseline.commit:
                raise ToolRefusedError("A captured main moved; preserve and recapture inputs")
            for head in source.heads:
                head.current()
        self.require_substantive(repositories)

    def resume_publication(
        self, run: InputPublication, context: TeamContext, carrier: PullRequestHead
    ) -> CombinedInput:
        return run.capture(context, self, carrier)

    def require_recapture(self, core: Repo, carrier: PullRequestHead) -> None:
        """A known published prospective cut may be replaced explicitly, never recreated."""
        self.require_repositories(
            self.declared_repositories(core, self.local.baseline.commit, carrier.head)
        )
        self.bind(core, carrier)
        if self.from_message(WorkBranches(core.checkout).commit_message(carrier.head)) != (self,):
            raise ToolRefusedError("The published prospective cut does not contain this exact input")

    def bind(self, repository: Repo, carrier: PullRequestHead) -> CombinedInput:
        """Bind the actual publication without substituting another source tree or history."""
        if carrier.repo != self.predecessor.repository:
            raise ToolRefusedError("The published carrier belongs to another repository")
        self.local.require_origin(repository)
        repository.acquire(self.predecessor.commit, carrier.head)
        if not Repo.descends(repository.checkout, self.predecessor.commit, carrier.head):
            raise ToolRefusedError("The published carrier dropped its prepared predecessor")
        self.local.require_ancestry(repository, carrier.head)
        return CombinedInput(self.sources, carrier.identity)


@dataclass(frozen=True)
class CombinedInput(SubstantiveInput):
    """The actual separate carrier before publication of this immutable source capture."""

    carrier: PullRequestHead

    @property
    def combined(self) -> bool:
        return True

    def carriers(self) -> tuple[IntegrationInput, ...]:
        return (self,)

    def pinned_sources(self, repository: RepositoryName) -> tuple[IntegrationSource, ...]:
        return self.sources

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.local.repository != self.carrier.repo:
            raise MalformedRecord("The local source must belong to the captured carrier repository")
        if len(self.carrier.head) != 40 or self.carrier.head != self.carrier.head.lower():
            raise MalformedRecord(
                "A combined carrier requires its full prepublication Git identity"
            )
        if any(
            head.same_pull_request(self.carrier) for source in self.sources for head in source.heads
        ):
            raise MalformedRecord("The combined carrier is separate from every source member")

    def resume_publication(
        self, run: InputPublication, context: TeamContext, carrier: PullRequestHead
    ) -> CombinedInput:
        core = Repo.of(run.checkout.path)
        commit = Repo.git(run.checkout.path, "rev-parse", "HEAD").strip()
        if carrier.head not in (self.carrier.head, commit):
            raise ToolRefusedError("The published carrier moved outside this captured attempt")
        self.require_carrier(core, PullRequestHead(core.github, carrier.pr, commit))
        run.retain_capture(self, commit)
        return self

    def validate_published(self, repositories: tuple[Repo, ...], carrier: PullRequestHead) -> None:
        self._validate_current(repositories, carrier, carrier.published_after(self.carrier))

    def require_local_candidate(self, core: Repo, carrier: PullRequestHead) -> None:
        self.local.require_origin(core)
        self.local.require_heads(carrier)

    def require_carrier(self, core: Repo, carrier: PullRequestHead) -> None:
        """Review and hosted merge checks consume this same Git-bound carrier distinction."""
        if not self.carrier.same_pull_request(carrier) or core.github != self.carrier.repo:
            raise ToolRefusedError("This is not the captured combined carrier pull request")
        reader = WorkBranches(core.checkout)
        core.acquire(self.carrier.head, carrier.head)
        source = self.local
        source.require_origin(core)
        self.require_repositories(
            self.declared_repositories(core, source.baseline.commit, carrier.head)
        )
        source.require_ancestry(core, self.carrier.head)
        source.require_ancestry(core, carrier.head)
        if reader.commit_parents(carrier.head) != (self.carrier.head,):
            raise ToolRefusedError(
                "The published carrier must directly preserve its captured prepublication head"
            )
        if self.from_message(reader.commit_message(carrier.head)) != (self,):
            raise ToolRefusedError(
                "The current carrier does not contain this exact canonical input"
            )
        StackSources.require_compatible(
            PrivateUvProject.decode(
                Repo.git(core.checkout, "show", f"{source.baseline.commit}:stack/pyproject.toml")
            ),
            PrivateUvProject.decode(
                Repo.git(core.checkout, "show", f"{carrier.head}:stack/pyproject.toml")
            ),
        )


class Side(DeclaredFamily, affix="Side"):
    """One compared source cut; selection and materialized path belong to this member."""

    @classmethod
    def sides(cls) -> tuple[type[Side], ...]:
        return cls.members_with(cls)

    @classmethod
    def checkout(cls, directory: Path, repository: RepositoryName) -> Path:
        return directory / cls.declared_name / Path(*repository.key)

    @classmethod
    def require_materialized(cls, directory: Path, source: IntegrationSource) -> Path:
        checkout = cls.checkout(directory, source.repository)
        source.require_origin(Repo.of(checkout))
        if dirty := Repo.git(checkout, "status", "--porcelain").strip():
            raise ToolRefusedError(f"Preserve dirty materialized source: {checkout}:\n{dirty}")
        cls.require_revision(checkout, source)
        return checkout

    @staticmethod
    @abstractmethod
    def require_revision(checkout: Path, source: IntegrationSource) -> None:
        """Each side proves its own exact captured cut."""


class BaseSide(Side):
    @staticmethod
    def require_revision(checkout: Path, source: IntegrationSource) -> None:
        if Repo.git(checkout, "rev-parse", "HEAD").strip() != source.baseline.commit:
            raise ToolRefusedError("Materialized baseline is not the captured main commit")


class HeadSide(Side):
    @staticmethod
    def require_revision(checkout: Path, source: IntegrationSource) -> None:
        if Repo.git(checkout, "rev-parse", "HEAD^{tree}").strip() != source.candidate_tree:
            raise ToolRefusedError("Materialized candidate is not the captured source tree")
        if not Repo.descends(checkout, source.baseline.commit, "HEAD") or any(
            not Repo.descends(checkout, head.head, "HEAD") for head in source.heads
        ):
            raise ToolRefusedError("Materialized candidate dropped a captured source parent")


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
    """Source review records a decision without requesting a merge."""

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
    forge_state: ClassVar[str] = "open"

    @classmethod
    def from_forge(cls, repo: RepositoryName, pr: int, response: str) -> OpenPullRequestHead:
        head, base, draft, state = response.split()
        if state != cls.forge_state:
            raise ToolRefusedError(f"#{pr} is no longer an open source.")
        GitCommitText.decode(head)
        if len(head) != 40:
            raise ToolRefusedError("The forge head must be a full Git commit")
        return cls.drafted(draft)(repo, pr, head, base=base)

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

    def accepted_orderless_sources(
        self, record: HeadReviewRecord
    ) -> tuple[PullRequestHead, ...]:
        """Only an orderless relationship supplies authority outside current Todo links."""
        return ()


class HeadReviewOrder(HeadReviewRelation, declared_name=None):
    """The input that authorizes a head review's optional order change."""

    def prepared(self, goals: Goals, problems: dict[str, str]) -> tuple[HeadReviewOrder, ...]:
        """A resolved relationship can authenticate; a refused selection contributes errors."""
        return (self,)

    @staticmethod
    def holding(todos: Sequence[Todo], request: PullRequestHead) -> tuple[Todo, ...]:
        """The open orders whose current declaration holds this head, held or released."""
        return tuple(todo for todo in todos if todo.order.holds_head(request))

    def require_reviewer(
        self, goals: Goals, request: PullRequestHead, actor: ThreadIncarnation
    ) -> None:
        snapshot = goals.registry.snapshot()
        snapshot.team_project.require_scope(snapshot, actor, request.repo)

    @abstractmethod
    def record_review(
        self, goals: Goals, request: PullRequestHead, verdict: HeadVerdict, actor: ThreadIncarnation
    ) -> HeadReviewRecord: ...


@dataclass(frozen=True)
class PreparedHeadReview:
    """The current forge head and its checked status text, read once before recording."""

    current: ReadPullRequestHead
    subject: str


class HeadReview(DeclaredFamily, affix="HeadReview"):
    context: ClassVar[str] = "agent-comms/review"
    # GitHub's commit-status states, an external contract.
    state: ClassVar[str]
    # GitHub caps a status description at 140 characters.
    description_limit: ClassVar[int] = 140

    @staticmethod
    def required_contexts() -> tuple[str, ...]:
        return (HeadReview.context,)

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


class MergeHeadReview(HeadReview, declared_name=None):
    """An explicit permission to merge, separate from accepting source for combination."""

    context = "agent-comms/merge"

    @classmethod
    def required_contexts(cls) -> tuple[str, ...]:
        return (*super().required_contexts(), cls.context)

    @classmethod
    def require_combined(
        cls, request: PullRequestHead, repositories: tuple[Repo, ...]
    ) -> IntegrationInput:
        repository = Repo.selected(repositories, request.repo.to_wire())
        captured = IntegrationInput.from_message(
            WorkBranches(repository.checkout).commit_message(request.head)
        )
        if not captured:
            raise ToolRefusedError(
                "Only a captured combined carrier may merge; accept individual source with judge ok"
            )
        (inputs,) = captured
        inputs.validate(repositories, request)
        inputs.require_source_reviews()
        return inputs

    @classmethod
    def require_merge(
        cls, request: PullRequestHead, repositories: tuple[Repo, ...]
    ) -> IntegrationInput:
        inputs = cls.require_combined(request, repositories)
        cls.require_permission(request)
        return inputs

    @classmethod
    def require_permission(cls, request: PullRequestHead) -> None:
        if not (
            HeadReview.accepted(request.repo, request.head)
            and cls.accepted(request.repo, request.head)
        ):
            raise ToolRefusedError(
                "The exact combined carrier has no explicit reviewed merge promotion"
            )


@dataclass(frozen=True)
class PromotedHeadReview(MergeHeadReview):
    """A forge permission, never a persisted source-review verdict."""

    state = "success"

    @classmethod
    def allows_integration(cls) -> bool:
        return True

    def description(self, reviewer: str, subject: str) -> str:
        return f"Explicitly promoted by {reviewer} on {subject}"

    def require_sources(
        self, goals: Goals, request: PullRequestHead, reviewer: ThreadIncarnation
    ) -> None:
        snapshot = goals.registry.snapshot()
        project = snapshot.team_project.require_scope(snapshot, reviewer, request.repo)
        inputs = self.require_combined(request, project.repos)
        goals.todos.require_input_sources(inputs)

    def enqueue(self, record: HeadReviewRecord, repositories: tuple[Repo, ...]) -> MergeArm:
        request = record.request
        if not HeadReview.accepted(request.repo, request.head):
            raise ToolRefusedError("The exact combined source review must succeed before promotion")
        current = request.current()
        self.require_combined(request, repositories)
        self.post(request.repo, request.head, record.actor.name, f"#{request.pr}")
        try:
            self.require_merge(request, repositories)
        except ToolRefusedError, GitCommandError:
            UnpromotedHeadReview().post(
                request.repo, request.head, record.actor.name, f"#{request.pr}"
            )
            raise
        return current.arm()


@dataclass(frozen=True)
class UnpromotedHeadReview(MergeHeadReview):
    state = "pending"

    def description(self, reviewer: str, subject: str) -> str:
        return f"Not promoted by {reviewer} on {subject}"
