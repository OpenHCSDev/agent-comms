"""Branches holding work that is not on main, and what links each one.

Tristan's rule (2026-10-16): work on a problem is on main, or on a branch with
an open PR or an open issue linked to it; never on a branch with neither. This
module derives that answer on each read from the forge and from the checkout's
worktrees. Nothing here is stored.
"""

from __future__ import annotations

from agent_comms.field_codec import WireExclude

import subprocess
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from .code_lines import CodeLines
from .declared_family import DeclaredFamily
from .field_codec import GitCommitText, PathText, projected
from .repo import (
    BranchUpstream, Checkout, CheckoutHistory, DetachedHead, GitCommandError, GitHead,
    Repo, RepositoryReads, UnpushedHead,
)
from .repository_name import RepositoryName
from .wire_models import WireModel

if TYPE_CHECKING:
    from .closed_pull_requests import ClosedHeadScan
    from .registry_document import RegistrySnapshot


class WorkBranchError(ValueError):
    """The checkout or the forge could not answer."""


class CommitBase(DeclaredFamily):
    """A head's relationship to main, decoded from one Git merge-base exit."""

    @abstractmethod
    def missing(self, scan: ClosedHeadScan, head: str) -> tuple[str, ...]: ...

    @abstractmethod
    def description(self, count: int) -> str: ...


@dataclass(frozen=True)
class SharedCommitBase(CommitBase):
    commit: Annotated[str, GitCommitText]

    def missing(self, scan: ClosedHeadScan, head: str) -> tuple[str, ...]:
        return () if self.commit == head else scan.missing_after(self.commit, head)

    def description(self, count: int) -> str:
        return f'holds {count} commits main lacks'


@dataclass(frozen=True)
class UnrelatedCommitBase(CommitBase):
    def missing(self, scan: ClosedHeadScan, head: str) -> tuple[str, ...]:
        return scan.reader.commits_since(head, scan.published)

    def description(self, count: int) -> str:
        return f'has unrelated Git history: {count} commits cannot be compared with main'


@dataclass(frozen=True)
class UnservableCommitBase(CommitBase):
    """The forge names a head the remote no longer serves: nothing can be compared with main."""

    def missing(self, scan: ClosedHeadScan, head: str) -> tuple[str, ...]:
        return (head,)

    def description(self, count: int) -> str:
        return 'has a head commit origin no longer serves'


class BranchLink(DeclaredFamily, affix="BranchLink"):
    """Why a branch's work can be found again."""


@dataclass(frozen=True)
class PullRequestBranchLink(BranchLink):
    number: int


@dataclass(frozen=True)
class IssueBranchLink(BranchLink):
    number: int


class BranchCheckout(DeclaredFamily, affix="BranchCheckout"):
    """Where a branch is checked out; the owner is whoever works there."""

    def working(self) -> tuple[str, ...]:
        """The registered threads working in this checkout."""
        return ()


@dataclass(frozen=True)
class ThreadBranchCheckout(BranchCheckout):
    threads: tuple[str, ...]
    worktree: str

    def working(self) -> tuple[str, ...]:
        return self.threads


@dataclass(frozen=True)
class UnregisteredBranchCheckout(BranchCheckout):
    """Checked out in a worktree that no registered thread works in."""

    worktree: str


@dataclass(frozen=True)
class WorkBranch:
    name: str
    unmerged_commits: int
    committed_at: str
    links: tuple[BranchLink, ...]
    checkouts: tuple[BranchCheckout, ...]

    @projected(view=WireExclude)
    def linked(self) -> bool:
        return bool(self.links)


@dataclass(frozen=True)
class UnparsedSource:
    """A changed src module whose pushed text Python cannot parse: conflict markers, or a
    half-saved edit. It is that branch's fact on the board, never an error out of the pass."""

    path: str
    line: int
    reason: str

    @classmethod
    def of(cls, path: str, error: SyntaxError) -> UnparsedSource:
        return cls(path, error.lineno or 0, error.msg)

    def listing(self) -> str:
        return f"{self.path}:{self.line} ({self.reason})"


@dataclass(frozen=True)
class SourceGrowth:
    """Code lines each changed src module gains between two commits."""

    files: tuple[tuple[str, int], ...]
    unparsed: tuple[UnparsedSource, ...] = ()
    limit: ClassVar[int] = 40
    tree: ClassVar[str] = "src/"

    @classmethod
    @lru_cache(maxsize=512)
    def capture(cls, checkout: Path, base: str, head: str, files: frozenset[str], *,
                timeout: float) -> SourceGrowth:
        """Immutable tree growth, cached without retaining a pass's branch reader."""
        paths = tuple(path for path in sorted(files)
                      if path.startswith(cls.tree) and path.endswith(".py"))
        if not paths:
            return cls(())
        # Only a tree-proved added/deleted side contributes zero, never a missing blob.
        sources = tuple(
            (path, blob, weight)
            for commit, weight in ((base, -1), (head, 1))
            for path, blob in Repo.tree_objects(checkout, commit, paths).items()
        )
        contents = Repo.blobs(checkout, *(blob for _path, blob, _weight in sources),
                              timeout=timeout)
        growth = dict.fromkeys(paths, 0)
        unparsed: list[UnparsedSource] = []
        for (path, _blob, weight), content in zip(sources, contents, strict=True):
            try:
                growth[path] += weight * CodeLines.of(content.decode())
            except SyntaxError as error:
                # A pushed branch is foreign text: the parse refusal is the branch's fact.
                unparsed.append(UnparsedSource.of(path, error))
        return cls(tuple(growth.items()), tuple(unparsed))

    @property
    def total(self) -> int:
        return sum(lines for _path, lines in self.files)

    @property
    def exceeds(self) -> bool:
        return self.total > self.limit

    def growing(self) -> tuple[str, ...]:
        return tuple(path for path, lines in self.files if lines > 0)

    def listing(self) -> str:
        return ", ".join(f"{path} {lines:+d}" for path, lines in self.files if lines)

    def require_parsed(self) -> None:
        """A head is judged only when every changed module parses."""
        if self.unparsed:
            raise WorkBranchError("Changed src does not parse: "
                                  + ", ".join(source.listing() for source in self.unparsed))


@dataclass(frozen=True)
class BranchDifference:
    """One branch against an immutable origin/main cut, shared by its consumers."""

    repository: RepositoryName
    branch: str
    head: str
    moved_at: float
    main: str
    behind: int
    files: frozenset[str]
    growth: SourceGrowth
    unpublished: tuple[UnpushedHead, ...]

    @property
    def changes_source(self) -> bool:
        """Any file under the source tree, not only the Python modules growth counts."""
        return any(path.startswith(SourceGrowth.tree) for path in self.files)


class ForgeAnswer(WireModel):
    """GitHub GraphQL answers keep GitHub's camelCase field names."""

    model_config = ConfigDict(strict=True, alias_generator=to_camel, extra="forbid")


class PageInfo(ForgeAnswer):
    has_next_page: bool
    end_cursor: str | None

    @property
    def following(self) -> tuple[tuple[str, ...], ...]:
        """The next page's cursor arguments, or none after the last page."""
        return ((self.end_cursor,),) if self.has_next_page else ()


class Connection[Node](ForgeAnswer):
    nodes: tuple[Node, ...]
    page_info: PageInfo | None = None


class ForgePullRequest(ForgeAnswer):
    number: int
    head_ref_name: str
    is_cross_repository: bool

    @property
    def branch_links(self) -> tuple[tuple[str, PullRequestBranchLink], ...]:
        # A fork's identically spelled branch is not this origin's branch.
        return (
            ()
            if self.is_cross_repository
            else ((self.head_ref_name, PullRequestBranchLink(self.number)),)
        )


class ForgeRefName(ForgeAnswer):
    name: str


class ForgeLinkedBranch(ForgeAnswer):
    ref: ForgeRefName


class ForgeIssue(ForgeAnswer):
    number: int
    linked_branches: Connection[ForgeLinkedBranch]


class PullsRepository(ForgeAnswer):
    pull_requests: Connection[ForgePullRequest]

    @property
    def connection(self) -> Connection[ForgePullRequest]:
        return self.pull_requests


class IssuesRepository(ForgeAnswer):
    issues: Connection[ForgeIssue]

    @property
    def connection(self) -> Connection[ForgeIssue]:
        return self.issues


class ForgeData[Repository](ForgeAnswer):
    repository: Repository


class ForgeQuery[Repository](ForgeAnswer):
    data: ForgeData[Repository]


@dataclass(frozen=True)
class BranchComparison:
    base: str
    head: str
    paths: tuple[Annotated[Path, PathText], ...]


@dataclass(frozen=True)
class WorkBranches:
    """One repository's branches with commits that main does not have."""

    checkout: Path
    reads: RepositoryReads = field(default_factory=RepositoryReads, compare=False, repr=False,
                                   kw_only=True)
    _remote_heads: dict[str, dict[str, str]] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    main: ClassVar[str] = "main"
    timeout: ClassVar[float] = 60.0

    _PULLS: ClassVar[str] = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { number headRefName isCrossRepository }
    }
  }
}"""
    _ISSUES: ClassVar[str] = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { number linkedBranches(first: 20) { nodes { ref { name } } } }
    }
  }
}"""

    def _gh(self, *arguments: str) -> str:
        result = subprocess.run(
            ("gh", *arguments),
            cwd=self.checkout,
            capture_output=True,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip() or "gh failed"
            raise WorkBranchError(f"gh {' '.join(arguments[:2])} in {self.checkout}: {detail}")
        return result.stdout.decode()

    def _git(self, *arguments: str, input: str = "") -> str:
        return Repo.git(self.checkout, *arguments, timeout=self.timeout, input=input)

    @cached_property
    def history(self) -> CheckoutHistory:
        return self.reads.history_for(Checkout(self.checkout))

    @cached_property
    def head(self) -> GitHead:
        return self.history.head_for(Checkout(self.checkout))

    def read_checkout(self) -> CheckoutRead:
        """Whether git can open this checkout at all; the one probe every reader asks first."""
        try:
            self.history.require_read()
        except GitCommandError as error:
            return UnreadableCheckoutRead(self.checkout, str(error))
        return ReadableCheckoutRead(self)

    def uncommitted(self) -> tuple[Path, ...]:
        """Each tracked file changed since HEAD and each untracked file not ignored.

        A submodule is pinned package data, never a unit's edit, so its git state is not read:
        one checkout with a broken submodule link must not fail every reader of every unit.
        """
        listed = (
            *self._git(
                "--no-optional-locks",
                "diff",
                "--name-only",
                "-z",
                "--ignore-submodules=all",
                "HEAD",
            ).split("\0"),
            *self._git(
                "--no-optional-locks", "ls-files", "--others", "--exclude-standard", "-z"
            ).split("\0"),
        )
        return tuple(self.checkout / name for name in listed if name)

    @property
    def local_work(self) -> bool:
        """Uncommitted files or HEAD commits absent from every published remote ref."""
        return bool(self.uncommitted()) or bool(self.history.unpublished(self.head.revision()))

    def remote_heads(self, remote: str, reference: str) -> tuple[str, ...]:
        """One complete published-ref cut per remote for this reader, not one call per branch."""
        if remote in self._remote_heads:
            return self.selected_heads(remote, reference)
        with Repo.acquiring():
            heads = {
                reference: head
                for head, reference in (line.split() for line in self._git(
                    "ls-remote", "--refs", remote,
                ).splitlines())
            }
        self._remote_heads[remote] = heads
        return self.selected_heads(remote, reference)

    def selected_heads(self, remote: str, reference: str) -> tuple[str, ...]:
        """Select locally; absence in the captured remote cut is a deleted publication."""
        if remote not in self._remote_heads:
            raise WorkBranchError(
                f"Unprepared remote head {remote}:{reference} in {self.checkout}")
        heads = self._remote_heads[remote]
        return (heads[reference],) if reference in heads else ()

    def _pull_request_base(self, base: str) -> str:
        published = self.selected_heads(Repo.remote, f"refs/heads/{base}")
        if not published:
            raise WorkBranchError(f"Origin has no pull request base {base!r} in {self.checkout}")
        (base_head,) = published
        return base_head

    def prepare_pull_request(self, base: str, head: str) -> None:
        with Repo.acquiring():
            self.remote_heads(Repo.remote, f"refs/heads/{base}")
            Repo.require_commits(self.checkout, Repo.remote, self._pull_request_base(base), head)
            self.history.include(self._pull_request_base(base), head)

    def pull_request_base(self, base: str, head: str) -> str:
        base_head = self._pull_request_base(base)
        self.history.commit(base_head)
        self.history.commit(head)
        return base_head

    @cached_property
    def upstreams(self) -> dict[str, BranchUpstream]:
        return {record.branch: record for record in (
            DetachedHead(self.head.reference),
            *(upstream for reference in self.history.refs.values()
              for upstream in reference.upstreams()),
        )}

    def prepare_upstreams(self) -> None:
        """Acquire all declared publications in one remote cut and one object batch per remote."""
        with Repo.acquiring():
            references: dict[str, set[str]] = {}
            for upstream in self.upstreams.values():
                for remote, reference in upstream.publication_inputs():
                    references.setdefault(remote, set()).add(reference)
            for remote, refs in references.items():
                Repo.enter_phase()
                heads = tuple(dict.fromkeys(
                    head for reference in sorted(refs)
                    for head in self.remote_heads(remote, reference)))
                Repo.require_commits(self.checkout, remote, *heads)
                self.history.include(*heads)

    def pull_request_patch_id(self, base: str, head: str) -> str:
        base_head = self.pull_request_base(base, head)
        patch = self._git(
            "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
            f"{base_head}...{head}", "--",
        )
        return self._git("patch-id", "--verbatim", input=patch).partition(" ")[0].strip()

    def pull_request_diff(self, base: str, head: str) -> BranchComparison:
        base_head = self.pull_request_base(base, head)
        paths = tuple(
            Path(name)
            for name in self._git(
                "diff",
                "--name-only",
                "-z",
                f"{base_head}...{head}",
                "--",
            ).split("\0")
            if name
        )
        return BranchComparison(base_head, head, paths)

    def commit_base(self, main: str, head: str) -> CommitBase:
        """Git's best common ancestor; exit 1 means the histories share none."""
        bases = self.history.merge_base(main, head)
        return SharedCommitBase(bases[0]) if bases else UnrelatedCommitBase()

    def commits_since(self, head: str, *excluded: str) -> tuple[str, ...]:
        return tuple(commit.sha for commit in self.history.commits((head,), *excluded)
                     if len(commit.parents) < 2)

    def whole_patch_ids(self, base: str, head: str) -> frozenset[str]:
        """One stable fingerprint of the entire immutable diff, including multi-commit squash."""
        output = Repo.git_pipeline(self.checkout,
            ('diff', '--full-index', '--no-color', '--no-renames', '--no-ext-diff',
             '--no-textconv', base, head),
            ('patch-id', '--stable'), timeout=self.timeout, input='')
        return frozenset(line.split()[0] for line in output.splitlines())

    def patch_ids(self, commits: tuple[str, ...]) -> dict[str, frozenset[str]]:
        """Decode Git's patch fingerprints once; an empty commit has an empty set of patches."""
        patches: dict[str, frozenset[str]] = {commit: frozenset() for commit in commits}
        if commits:
            fingerprints = Repo.git_pipeline(self.checkout,
                ('diff-tree', '--stdin', '--root', '-r', '-p', '--full-index', '--no-color',
                 '--no-renames', '--no-ext-diff', '--no-textconv'),
                ('patch-id', '--stable'), timeout=self.timeout,
                input=''.join(f'{commit}\n' for commit in commits))
            for line in fingerprints.splitlines():
                patch, commit = line.split()
                patches[commit] = frozenset((patch,))
        return patches

    def require_upstream(self, branch: str) -> BranchUpstream:
        """A local branch must belong to this reader's actual prepared publication cut."""
        if branch not in self.upstreams:
            raise WorkBranchError(f"Unprepared branch {branch!r} in {self.checkout}")
        upstream = self.upstreams[branch]
        for published in self.published(upstream):
            self.history.commit(published)
        return upstream

    def published(self, upstream: BranchUpstream) -> tuple[str, ...]:
        """The upstream's heads in this reader's prepared remote cut; none when unpublished."""
        return tuple(head for remote, reference in upstream.publication_inputs()
                     for head in self.selected_heads(remote, reference))

    def unpushed(self, branch: str, head: str) -> tuple[UnpushedHead, ...]:
        """Capture publication at the source reader, never through another clone's refs."""
        self.history.commit(head)
        upstream = self.require_upstream(branch)
        return upstream.unpushed(self.history, head, self.published(upstream))

    @cached_property
    def repository(self) -> tuple[str, str]:
        repository = Repo.of(self.checkout).github
        return repository.owner, repository.name

    @cached_property
    def origin_main(self) -> str:
        return self.history.resolve(f"refs/remotes/{Repo.remote}/{self.main}")

    def difference(self, main: str) -> BranchDifference:
        """Git owns ancestry and changed paths; pin both commits before comparing."""
        movement = self._git(
            "reflog",
            "show",
            "-1",
            "--date=unix",
            "--format=%H%x00%gD",
            "HEAD",
        ).strip()
        try:
            head, selector = movement.split("\0")
            moved_at = float(selector.rsplit("@{", 1)[1].removesuffix("}"))
        except (ValueError, IndexError) as unread:
            raise WorkBranchError(
                f"HEAD reflog in {self.checkout} has no readable movement time"
            ) from unread
        branch = self.head.branch
        bases = self.history.merge_base(main, head)
        if not bases:
            raise WorkBranchError(f"Unrelated main and HEAD in {self.checkout}")
        base = bases[0]
        behind = self.history.count(main, base)
        files = self.changed(base, head)
        return BranchDifference(
            RepositoryName(*self.repository), branch, head, moved_at, main, behind, files,
            SourceGrowth.capture(self.checkout, base, head, files, timeout=self.timeout),
            self.unpushed(branch, head),
        )

    def changed(self, base: str, head: str) -> frozenset[str]:
        return frozenset(path for path in self._git(
            "diff", "--name-only", "-z", "--no-renames", base, head).split("\0") if path)

    def reviewed_growth(self, pr: int, head: str, branch: str) -> SourceGrowth:
        """A pull request head against its declared base branch, so a request stacked on
        another is charged only its own lines; fetched without touching FETCH_HEAD."""
        self._git("fetch", "-q", "--no-write-fetch-head", Repo.remote,
                  branch, f"refs/pull/{pr}/head")
        base = self._git("merge-base", f"refs/remotes/{Repo.remote}/{branch}", head).strip()
        return SourceGrowth.capture(self.checkout, base, head, self.changed(base, head),
                                    timeout=self.timeout)

    def _pages(self, query: str, answer: type[ForgeQuery], **variables: str):
        pages: list[tuple[str, ...]] = [()]
        for after in pages:
            arguments = ["api", "graphql", "-f", f"query={query}"]
            for key, value in {**variables, **dict(zip(("after",), after, strict=False))}.items():
                arguments += ["-F", f"{key}={value}"]
            connection = answer.model_validate_json(self._gh(*arguments)).data.repository.connection
            yield from connection.nodes
            pages.extend(connection.page_info.following)

    def worktrees(self) -> dict[str, tuple[str, ...]]:
        """Branch name -> worktree paths that have it checked out."""
        checked: dict[str, list[str]] = {}
        for head in self.history.heads.values():
            for branch in head.checked_branch():
                checked.setdefault(branch, []).append(str(head.checkout.path))
        return {branch: tuple(paths) for branch, paths in checked.items()}

    def checkouts(self, snapshot: RegistrySnapshot, branch: str,
                  worktrees: Mapping[str, tuple[str, ...]]) -> tuple[BranchCheckout, ...]:
        """Every checkout of ``branch`` among ``worktrees`` (as ``worktrees()`` reads them),
        each naming the registered threads that work there."""
        return tuple(self._checkout(snapshot, path) for path in worktrees.get(branch, ()))

    @staticmethod
    def _checkout(snapshot: RegistrySnapshot, worktree: str) -> BranchCheckout:
        resolved = str(Path(worktree).resolve())
        threads = tuple(
            sorted(
                thread.name
                for thread in snapshot.threads.values()
                if not snapshot.statuses[thread.name].archived
                and str(thread.worktree.path.expanduser().resolve()) == resolved
            )
        )
        if not threads:
            return UnregisteredBranchCheckout(worktree)
        return ThreadBranchCheckout(threads, worktree)

    def read(self, snapshot: RegistrySnapshot) -> tuple[WorkBranch, ...]:
        """Every branch with commits not on main, most recently committed first."""
        owner, name = self.repository
        with Repo.acquiring():
            refs = {
                ref.removeprefix("refs/heads/"): oid
                for oid, ref in (
                    line.split() for line in self._git("ls-remote", "--heads", Repo.remote).splitlines()
                )
            }
            Repo.require_commits(self.checkout, Repo.remote, *refs.values())
        main = refs[self.main]
        # Git already owns commit ancestry. Ask it once for all locally named
        # commits, rather than asking GitHub to compare every branch separately.
        commits = {
            oid: (int(ahead), date)
            for oid, date, ahead, _behind in (
                line.split()
                for line in self._git(
                    "for-each-ref",
                    f"--format=%(objectname) %(committerdate:iso-strict) %(ahead-behind:{main})",
                    "refs/heads",
                    f"refs/remotes/{Repo.remote}",
                ).splitlines()
            )
        }
        links: dict[str, list[BranchLink]] = {}
        for pull in self._pages(self._PULLS, ForgeQuery[PullsRepository], owner=owner, name=name):
            for branch, link in pull.branch_links:
                links.setdefault(branch, []).append(link)
        for issue in self._pages(
            self._ISSUES, ForgeQuery[IssuesRepository], owner=owner, name=name
        ):
            for linked in issue.linked_branches.nodes:
                links.setdefault(linked.ref.name, []).append(IssueBranchLink(issue.number))
        worktrees = self.worktrees()
        branches = []
        for branch, oid in refs.items():
            if oid not in commits:
                # A branch published since the last local fetch has no local
                # ref yet. Compare its captured object, never an older ref.
                commits[oid] = (
                    int(self._git("rev-list", "--count", f"{main}..{oid}")),
                    self._git("show", "-s", "--format=%cI", oid).strip(),
                )
            ahead, date = commits[oid]
            if branch != self.main and ahead:
                branches.append(
                    WorkBranch(
                        branch,
                        ahead,
                        datetime.fromisoformat(date)
                        .astimezone(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        tuple(links.get(branch, ())),
                        self.checkouts(snapshot, branch, worktrees),
                    )
                )
        return tuple(sorted(branches, key=lambda branch: branch.committed_at, reverse=True))


class CheckoutRead(DeclaredFamily, affix="CheckoutRead"):
    """What git can say about one unit's checkout.

    An unreadable checkout is that unit's own state, reported by its standing rule;
    it never fails a pass that reads every other unit.
    """

    @property
    @abstractmethod
    def holds_work(self) -> bool: ...

    @abstractmethod
    def uncommitted(self) -> tuple[Path, ...]: ...

    @abstractmethod
    def errors(self) -> tuple[str, ...]: ...

    @abstractmethod
    def worktrees(self) -> tuple[Path, ...]:
        """Every checkout of this repository, as Git lists it."""


@dataclass(frozen=True)
class ReadableCheckoutRead(CheckoutRead):
    branches: WorkBranches

    @property
    def holds_work(self) -> bool:
        return self.branches.local_work

    def uncommitted(self) -> tuple[Path, ...]:
        return self.branches.uncommitted()

    def errors(self) -> tuple[str, ...]:
        return ()

    def worktrees(self) -> tuple[Path, ...]:
        return tuple(self.branches.history.heads)


@dataclass(frozen=True)
class UnreadableCheckoutRead(CheckoutRead):
    """Git refuses the checkout: whatever it holds stays with its unit until the unit repairs it."""

    checkout: Path
    error: str

    @property
    def holds_work(self) -> bool:
        return True

    def uncommitted(self) -> tuple[Path, ...]:
        return ()

    def errors(self) -> tuple[str, ...]:
        return (self.error,)

    def worktrees(self) -> tuple[Path, ...]:
        """Git cannot list its siblings, so the checkout stands as its own until repaired."""
        return (self.checkout.resolve(),)
