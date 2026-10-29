"""Branches holding work that is not on main, and what links each one.

Tristan's rule (2026-10-16): work on a problem is on main, or on a branch with
an open PR or an open issue linked to it; never on a branch with neither. This
module derives that answer on each read from the forge and from the checkout's
worktrees. Nothing here is stored.
"""

from __future__ import annotations

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
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import GitCommitText, PathText, projected
from .repo import GitCommandError, Repo
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

    @projected(view="wire")
    def linked(self) -> bool:
        return bool(self.links)


@dataclass(frozen=True)
class SourceGrowth:
    """Code lines each changed src module gains between two commits."""

    files: tuple[tuple[str, int], ...]
    limit: ClassVar[int] = 40
    tree: ClassVar[str] = "src/"

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
class UnpushedHead:
    head: str
    commits: int = field(compare=False)
    upstream: str = field(compare=False)


@dataclass(frozen=True)
class BranchComparison:
    base: str
    head: str
    paths: tuple[Annotated[Path, PathText], ...]


@dataclass(frozen=True)
class BranchUpstream(DeclaredFamily):
    """Git's upstream decoded once; each member owns acquisition and publication answers."""

    branch: str

    @classmethod
    def from_git(cls, row: str) -> BranchUpstream:
        branch, tracking, remote, reference = row.split("\0")
        if not tracking:
            return NoUpstream(branch)
        if remote == Repo.local_remote:
            return LocalUpstream(branch, tracking)
        return PublishedUpstream(branch, tracking, remote, reference)

    def prepare(self, reader: WorkBranches) -> None:
        """No configured publication means no remote acquisition."""

    def tracking_name(self) -> str:
        return ""

    def unpushed(self, reader: WorkBranches, head: str) -> tuple[UnpushedHead, ...]:
        # A local, absent or deleted upstream cannot make its stale tracking ref publication.
        tracking = self.tracking_name()
        remotes = tuple(ref for ref in reader._git(
            "for-each-ref", "--format=%(refname)", "refs/remotes/",
        ).splitlines() if ref != tracking)
        count = int(reader._git("rev-list", "--count", head, "--not", *remotes))
        return (UnpushedHead(head, count, tracking),) if count else ()


class NoUpstream(BranchUpstream):
    """This local branch declares no upstream."""


@dataclass(frozen=True)
class TrackedUpstream(BranchUpstream, declared_name=None):
    tracking: str

    def tracking_name(self) -> str:
        return self.tracking


class LocalUpstream(TrackedUpstream):
    """Git's dot remote names another local branch, never a publication."""


@dataclass(frozen=True)
class PublishedUpstream(TrackedUpstream):
    remote: str
    reference: str

    def prepare(self, reader: WorkBranches) -> None:
        Repo.require_commits(reader.checkout, self.remote,
                             *reader.remote_heads(self.remote, self.reference))

    def unpushed(self, reader: WorkBranches, head: str) -> tuple[UnpushedHead, ...]:
        published = reader.selected_heads(self.remote, self.reference)
        if published:
            (remote_head,) = published
            Repo.require_local_commits(reader.checkout, remote_head)
            count = int(reader._git("rev-list", "--count", f"{remote_head}..{head}"))
            return (UnpushedHead(head, count, self.tracking),) if count else ()
        return super().unpushed(reader, head)


@dataclass(frozen=True)
class WorkBranches:
    """One repository's branches with commits that main does not have."""

    checkout: Path
    _remote_heads: dict[tuple[str, str], tuple[str, ...]] = field(
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

    def read_checkout(self) -> CheckoutRead:
        """Whether git can open this checkout at all; the one probe every reader asks first."""
        try:
            self._git("--no-optional-locks", "rev-parse", "--git-dir")
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
        return bool(self.uncommitted()) or bool(
            self._git("rev-list", "--max-count=1", "HEAD", "--not", "--remotes")
        )

    def remote_heads(self, remote: str, reference: str) -> tuple[str, ...]:
        key = remote, reference
        if key not in self._remote_heads:
            self._remote_heads[key] = tuple(
                line.split()[0]
                for line in self._git(
                    "ls-remote",
                    "--refs",
                    remote,
                    reference,
                ).splitlines()
            )
        return self._remote_heads[key]

    def selected_heads(self, remote: str, reference: str) -> tuple[str, ...]:
        """Consume the prepared remote cut; an unexpected comparison never opens another cut."""
        return self._remote_heads[remote, reference]

    def _pull_request_base(self, base: str) -> str:
        published = self.selected_heads(Repo.remote, f"refs/heads/{base}")
        if not published:
            raise WorkBranchError(f"Origin has no pull request base {base!r} in {self.checkout}")
        (base_head,) = published
        return base_head

    def prepare_pull_request(self, base: str, head: str) -> None:
        self.remote_heads(Repo.remote, f"refs/heads/{base}")
        Repo.require_commits(self.checkout, Repo.remote, self._pull_request_base(base), head)

    def pull_request_base(self, base: str, head: str) -> str:
        base_head = self._pull_request_base(base)
        Repo.require_local_commits(self.checkout, base_head, head)
        return base_head

    @cached_property
    def upstreams(self) -> dict[str, BranchUpstream]:
        return {record.branch: record for record in (
            BranchUpstream.from_git(line) for line in self._git(
                "for-each-ref",
                "--format=%(refname:strip=2)%00%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)",
                "refs/heads/",
            ).splitlines()
        )}

    def require_prepared(self) -> None:
        """Object loss after acquisition is unreadable input, never a new fetch."""
        Repo.require_local_commits(self.checkout,
                                  *(head for heads in self._remote_heads.values() for head in heads))

    def prepare_upstreams(self) -> None:
        for upstream in self.upstreams.values():
            upstream.prepare(self)

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
        try:
            return SharedCommitBase(self._git('merge-base', main, head).strip())
        except GitCommandError as error:
            if not error.unrelated_history():
                raise
            return UnrelatedCommitBase()

    def commits_since(self, head: str, *excluded: str) -> tuple[str, ...]:
        return tuple(self._git('rev-list', '--no-merges', head,
                               '--not', *excluded).splitlines())

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

    def unpushed(self, difference: BranchDifference) -> tuple[UnpushedHead, ...]:
        """Compare this HEAD with its actual upstream, without trusting a stale tracking ref."""
        Repo.require_local_commits(self.checkout, difference.head)
        return self.upstreams[difference.branch].unpushed(self, difference.head)

    @cached_property
    def repository(self) -> tuple[str, str]:
        repository = Repo.of(self.checkout).github
        return repository.owner, repository.name

    @cached_property
    def origin_main(self) -> str:
        return self._git("rev-parse", f"refs/remotes/{Repo.remote}/{self.main}").strip()

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
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        base = self._git("merge-base", main, head).strip()
        behind = int(self._git("rev-list", "--count", f"{base}..{main}"))
        files = self.changed(base, head)
        return BranchDifference(
            RepositoryName(*self.repository), branch, head, moved_at, main, behind, files,
            self.source_growth(base, head, files),
        )

    def changed(self, base: str, head: str) -> frozenset[str]:
        return frozenset(path for path in self._git(
            "diff", "--name-only", "-z", "--no-renames", base, head).split("\0") if path)

    @lru_cache(maxsize=512)
    def source_growth(self, base: str, head: str, files: frozenset[str]) -> SourceGrowth:
        """Git owns both trees; each changed src module is counted once on each side.

        Both commits are immutable, so one pair is counted once per process: the
        heartbeat asks again every pass and the answer cannot move until a SHA does.
        """
        paths = tuple(path for path in sorted(files)
                      if path.startswith(SourceGrowth.tree) and path.endswith(".py"))
        if not paths:
            return SourceGrowth(())
        # Only a tree-proved added/deleted side contributes zero, never a missing blob.
        sources = tuple(
            (path, blob, weight)
            for commit, weight in ((base, -1), (head, 1))
            for path, blob in Repo.tree_objects(self.checkout, commit, paths).items()
        )
        contents = Repo.blobs(self.checkout, *(blob for _path, blob, _weight in sources),
                              timeout=self.timeout)
        growth = dict.fromkeys(paths, 0)
        for (path, _blob, weight), content in zip(sources, contents, strict=True):
            growth[path] += weight * CodeLines.of(content.decode())
        return SourceGrowth(tuple(growth.items()))

    def reviewed_growth(self, pr: int, head: str, branch: str) -> SourceGrowth:
        """A pull request head against its declared base branch, so a request stacked on
        another is charged only its own lines; fetched without touching FETCH_HEAD."""
        self._git("fetch", "-q", "--no-write-fetch-head", Repo.remote,
                  branch, f"refs/pull/{pr}/head")
        base = self._git("merge-base", f"refs/remotes/{Repo.remote}/{branch}", head).strip()
        return self.source_growth(base, head, self.changed(base, head))

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
        path = None
        head = ""
        for line in self._git("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ")
                head = ""
            elif line.startswith("HEAD "):
                head = line.removeprefix("HEAD ")
            elif line.startswith("branch refs/heads/") and path is not None and head.strip("0"):
                # Git retains a worktree's symbolic branch after its ref is deleted,
                # with an all-zero HEAD. That is not an existing local branch.
                checked.setdefault(line.removeprefix("branch refs/heads/"), []).append(path)
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
                if str(thread.worktree.path.expanduser().resolve()) == resolved
            )
        )
        if not threads:
            return UnregisteredBranchCheckout(worktree)
        return ThreadBranchCheckout(threads, worktree)

    def read(self, snapshot: RegistrySnapshot) -> tuple[WorkBranch, ...]:
        """Every branch with commits not on main, most recently committed first."""
        owner, name = self.repository
        refs = {
            ref.removeprefix("refs/heads/"): oid
            for oid, ref in (
                line.split() for line in self._git("ls-remote", "--heads", Repo.remote).splitlines()
            )
        }
        main = refs[self.main]
        # Git already owns commit ancestry. Ask it once for all locally named
        # commits, rather than asking GitHub to compare every branch separately.
        Repo.require_commits(self.checkout, Repo.remote, *refs.values())
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
        listed = self.branches._git("worktree", "list", "--porcelain", "-z")
        return tuple(
            Path(field.removeprefix("worktree ")).resolve()
            for field in listed.split("\0")
            if field.startswith("worktree ")
        )


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
