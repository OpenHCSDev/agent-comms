from __future__ import annotations

import heapq
import shutil
import string
from abc import (
    ABC,
    abstractmethod,
)
from bisect import bisect_right
from collections.abc import (
    Callable,
    Iterator,
)
from contextlib import (
    ExitStack,
    contextmanager,
)
from dataclasses import (
    dataclass,
    field,
)
from functools import cached_property
from itertools import accumulate
from pathlib import Path
from typing import (
    Annotated,
    ClassVar,
    Self,
)

from agent_comms.errors import MissingSourceLine
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GitCommitText,
    PresentMember,
    SingleFieldValue,
)
from agent_comms.projects.git_errors import GitCommandError, UnavailableCommitError
from agent_comms.projects.repo import Repo


class CheckoutHoldsWorkError(ValueError):
    """A checkout cannot be retired: it holds changes that removing it would lose."""

    def __init__(self, path: Path, changes: str):
        super().__init__(f"{path} holds uncommitted work:\n{changes}")


@dataclass(frozen=True)
class PinnedSubmodule:
    """One pinned submodule of a checkout: its working directory and git's stored repository.

    Git creates the store before it fetches into it, so a clone interrupted by a stopped
    unit or a timeout leaves a store with no repository, or a working directory whose `.git`
    points at a store that is gone, and every later update trusts it ("could not get a
    repository handle"). This is the one place that tells a usable store from a broken one."""

    checkout: Path
    name: str
    path: Path

    def contains(self, file: str) -> bool:
        """Whether the checkout-relative file belongs under this pinned module."""
        return (self.checkout / file).is_relative_to(self.path)

    def lines(self, commit: str, file: str, *, read: Callable[..., str] = Repo.git) -> tuple[str, ...]:
        """Read the parent's pinned object, independent of the module's working tree."""
        module = self.path.relative_to(self.checkout).as_posix()
        pinned = read(self.checkout, 'rev-parse', f'{commit}:{module}').strip()
        relative = (self.checkout / file).relative_to(self.path).as_posix()
        store = self.store
        read(store, '--git-dir', '.', 'rev-parse', '--verify',
                 f'{pinned}^{{commit}}')
        try:
            text = read(store, '--git-dir', '.',
                            'show', f'{pinned}:{relative}')
            return tuple(text.splitlines())
        except GitCommandError as error:
            if error.source_path_absent(pinned, relative):
                raise MissingSourceLine(
                    f'Source file {file} does not exist at {commit}.'
                ) from error
            raise

    @property
    def store(self) -> Path:
        return Path(
            Repo.git(
                self.checkout,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                f"modules/{self.name}",
            ).strip()
        )

    def answers(self, *question: str) -> bool:
        """Ask this module's own Git directory, never discover the enclosing checkout."""
        return Repo._run(self.checkout, '--git-dir', str(self.path / '.git'),
                         *question).returncode == 0

    def initialized(self) -> bool:
        """A checked-out commit is work to preserve, even without a fetch remote."""
        return (not self.path.is_symlink()
                and self.answers('rev-parse', '--verify', '--quiet', 'HEAD^{commit}'))

    def usable(self) -> bool:
        """An initialized module with the origin an explicit pin update can fetch from."""
        return self.initialized() and self.answers('remote', 'get-url', Repo.remote)

    def broken(self) -> bool:
        """An interrupted store, dangling Git pointer, copied directory or symlink;
        a valid initialized module is never clone debris."""
        if self.path.is_symlink():
            return True
        if self.initialized():
            return False
        occupied = self.path.is_dir() and any(self.path.iterdir())
        # Deinitialization leaves a valid store with local refs but no working tree.
        return ((self.store.exists() or occupied)
                and not Repo.store_serves(self.store, 'rev-parse', '--verify',
                                          '--quiet', 'HEAD^{commit}'))

    def checkout_pin(self) -> None:
        """Select this parent's pin only; nested modules have their own owners."""
        Repo.git(self.checkout, 'submodule', 'update', '--init', '--checkout', '--',
                 self.path.relative_to(self.checkout).as_posix(), timeout=300)

    def initialize(self) -> None:
        """Recover missing setup without changing a valid working module."""
        if not self.initialized():
            if self.broken():
                self.clear()
            self.checkout_pin()

    def prepare(self) -> None:
        """Keep this HEAD and its edits; initialize missing modules declared by it."""
        self.initialize()
        Checkout(self.path).prepare()

    def update(self) -> None:
        """Explicitly select the declared pin, retaining Git's dirty-work refusal."""
        self.initialize()
        if not self.usable():
            relative = self.path.relative_to(self.checkout).as_posix()
            Repo.git(self.checkout, 'submodule', 'init', '--', relative)
            url = Repo.git(self.checkout, 'config', '--get', f'submodule.{self.name}.url').strip()
            Repo.git(self.path, 'remote', 'add', Repo.remote, url)
        self.checkout_pin()
        Checkout(self.path).update_submodules()

    def clear(self) -> None:
        """Drop broken initialization so the module can clone from its declared source.
        initialize() never calls this for a valid checked-out commit."""
        if self.path.is_symlink():
            self.path.unlink()
        for directory in (self.store, self.path):
            if directory.exists():
                shutil.rmtree(directory)
        self.path.mkdir(parents=True)


class CheckoutStart(DeclaredFamily, affix="CheckoutStart"):
    """The declared way a tree starts, selected before it is added and prepared."""

    @abstractmethod
    def worktree_arguments(self, path: Path) -> tuple[str, ...]: ...

    @classmethod
    def of(cls, repository: Path, branch: str, base: str) -> CheckoutStart:
        if Repo.answers(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
            return KeptBranchStart(branch)
        return NewBranchStart(branch, base)


@dataclass(frozen=True)
class DetachedStart(CheckoutStart):
    revision: str

    def worktree_arguments(self, path: Path) -> tuple[str, ...]:
        return ("--detach", "-q", str(path), self.revision)


@dataclass(frozen=True)
class NewBranchStart(CheckoutStart):
    branch: str
    base: str

    def worktree_arguments(self, path: Path) -> tuple[str, ...]:
        return ("-q", "-b", self.branch, str(path), self.base)


@dataclass(frozen=True)
class KeptBranchStart(CheckoutStart):
    branch: str

    def worktree_arguments(self, path: Path) -> tuple[str, ...]:
        return ("-q", str(path), self.branch)


@dataclass(frozen=True)
class Checkout(SingleFieldValue, PresentMember):
    """A recorded working tree of one of our repositories.

    Its pinned submodules belong to the tree, and `git worktree
    add` leaves them empty even with submodule.recurse set, so every tree is added, prepared
    and retired here and nowhere else."""

    path: Path

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls(cls.path_from_input(FieldCodec.decode(str, data)))

    @staticmethod
    def path_from_input(path: Path | str) -> Path:
        """Keep empty input distinct from Path(''), which silently means the cwd."""
        if path == "":
            raise ValueError("Project path must not be empty")
        return Path(path)

    @property
    def test_environment(self) -> Path:
        """The interpreter prefix every order names for this tree's tests. Install syncs the
        live checkout's from the lock it moves to; nothing else writes it."""
        return self.path / ".venv"

    def resolved(self, *, strict: bool = False) -> Self:
        """Resolve this recorded path without imposing launch qualification."""
        return type(self)(self.path.expanduser().resolve(strict=strict))

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    @classmethod
    def git_roots(cls, *paths: Path) -> tuple[Path, ...]:
        """The given paths that are Git working trees, decided as Git decides: a `.git` file
        links a worktree, a `.git` folder holds HEAD. A plain or empty-`.git` folder is none."""
        return tuple(path for path in paths
                     if (path / ".git").is_file() or (path / ".git" / "HEAD").is_file())

    @classmethod
    def add(cls, repository: Path | str, path: Path | str, start: CheckoutStart) -> Self:
        Repo.git(repository, "worktree", "add", *start.worktree_arguments(Path(path)))
        return cls.prepared(Path(path))

    @classmethod
    def checked(cls, path: Path | str) -> Self:
        """Resolve a supplied project and require a Git checkout, never the shell's cwd."""
        checkout = cls(cls.path_from_input(path)).resolved(strict=True)
        if not cls.git_roots(checkout.path):
            raise ValueError(f"Project {checkout.path} is not a Git checkout")
        return checkout

    @classmethod
    def prepared(cls, path: Path) -> Self:
        """Prepare this checkout's pinned submodules before handing it to a consumer."""
        checkout = cls.checked(path)
        checkout.prepare()
        return checkout

    @cached_property
    def common_dir(self) -> Path:
        """Git's shared ref and object owner, independent of this worktree's HEAD."""
        return Repo.common_dir_of(self.path)

    def read(self) -> CheckoutHistory:
        """One operation's refs and commit graph; the caller owns this read's lifetime."""
        return CheckoutHistory(self)

    def submodules(self) -> tuple[PinnedSubmodule, ...]:
        if not (self.path / ".gitmodules").is_file():
            return ()
        listed = Repo.git(
            self.path, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"
        )
        return tuple(
            PinnedSubmodule(
                self.path, key.removeprefix("submodule.").removesuffix(".path"), self.path / value
            )
            for key, value in (line.split(" ", 1) for line in listed.splitlines())
        )

    def initialized_submodules(self) -> Iterator[PinnedSubmodule]:
        """Only existing module worktrees move; never initialize or replace their contents."""
        for module in self.submodules():
            if (module.path / '.git').is_file():
                yield module
                yield from Checkout(module.path).initialized_submodules()
            elif (module.path / '.git').exists():
                raise ValueError(f'Submodule {module.path} must have an absorbed Git directory')

    @contextmanager
    def relocating(self, destination: Path) -> Iterator[CheckoutTransfer]:
        """Move this exact linked worktree, repairing Git's submodule move limitation.

        Git refuses ``worktree move`` when modules are initialized. Rename retains the
        index and all bytes; ``worktree repair`` reconnects the parent, and each module's
        existing private Git directory gets its new worktree. Roll back before exposing
        a failed registration; no shared config or module contents are removed.
        """
        source = self.resolved(strict=True).path
        if source.parent != destination.parent or source == destination:
            raise ValueError('Checkout relocation requires distinct siblings in the allocation root')
        if not (source / '.git').is_file() or source.is_symlink():
            raise ValueError(f'{source} is not an isolated linked worktree')
        if destination.exists() or destination.is_symlink():
            raise ValueError(f'Checkout destination {destination} is occupied')
        common = self.common_dir
        modules = tuple(SubmoduleTransfer.capture(module, source, destination)
                        for module in self.initialized_submodules())
        transfer = CheckoutTransfer(source=source, destination=destination,
                                    branch=Repo.git(source, 'rev-parse', '--abbrev-ref', 'HEAD').strip(),
                                    head=Repo.git(source, 'rev-parse', 'HEAD').strip())
        with ExitStack() as rollback:
            source.rename(destination)
            rollback.callback(transfer.restore, common, modules)
            Repo.git(common, 'worktree', 'repair', str(destination))
            for module in modules:
                module.apply(destination)
            yield transfer
            rollback.pop_all()

    def prepare(self) -> None:
        """Initialize missing modules; preserve initialized HEADs and every local edit."""
        for module in self.submodules():
            module.prepare()

    def update_submodules(self) -> None:
        """Select this revision's pins after an explicit parent-version change."""
        for module in self.submodules():
            module.update()

    def held_work(self) -> str:
        """Every source change retirement would lose, including initialized submodules."""
        roots = (self.path, *(module.path for module in self.initialized_submodules()))
        return ''.join(f'{root}:\n{changes}' for root in roots
                       if (changes := Repo.git(root, "status", "--porcelain", "--ignore-submodules=none")))

    def retire(self) -> None:
        """Remove this worktree. Git refuses any tree holding submodules without --force,
        so this owner checks for held work itself and then forces."""
        held = self.held_work()
        if held:
            raise CheckoutHoldsWorkError(self.path, held)
        common = self.common_dir
        Repo.git(common.parent, "worktree", "remove", "--force", str(self.path))


@dataclass(frozen=True)
class UnpushedHead:
    head: str
    commits: int = field(compare=False)
    upstream: str = field(compare=False)


@dataclass(frozen=True)
class BranchUpstream(DeclaredFamily):
    """Git's upstream decoded once; each member owns acquisition and publication answers."""

    branch: str
    prefix: ClassVar[str] = "refs/heads/"

    @classmethod
    def from_git(cls, name: str, upstream: str, remote: str, reference: str) -> BranchUpstream:
        """Decode foreign for-each-ref fields before the reference enters our family."""
        branch = name.removeprefix(cls.prefix)
        if not upstream:
            return NoUpstream(branch)
        if remote == Repo.local_remote:
            return LocalUpstream(branch, upstream)
        return PublishedUpstream(branch, upstream, remote, reference)

    def publication_inputs(self) -> tuple[tuple[str, str], ...]:
        """Only published upstreams supply remote/reference acquisition requirements."""
        return ()

    def tracking_name(self) -> str:
        return ""

    def unpushed(self, history: CheckoutHistory, head: str,
                 published: tuple[str, ...]) -> tuple[UnpushedHead, ...]:
        # A local, absent or deleted upstream cannot make its stale tracking ref publication.
        tracking = self.tracking_name()
        count = history.unpublished(head, tracking)
        return (UnpushedHead(head, count, tracking),) if count else ()


class NoUpstream(BranchUpstream):
    """This local branch declares no upstream."""


class DetachedHead(BranchUpstream):
    """Git names a checkout on no branch `HEAD`, as during a rebase; it declares no upstream."""


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

    def publication_inputs(self) -> tuple[tuple[str, str], ...]:
        return ((self.remote, self.reference),)

    def unpushed(self, history: CheckoutHistory, head: str,
                 published: tuple[str, ...]) -> tuple[UnpushedHead, ...]:
        if published:
            # Ahead of the upstream is not unpushed: only a commit on no remote exists only locally.
            count = history.unpublished(head, "")
            return (UnpushedHead(head, count, self.tracking),) if count else ()
        return super().unpushed(history, head, published)


@dataclass(frozen=True)
class GitReference:
    """A repository ref; local branch members additionally own their decoded upstream."""

    name: str
    commit: Annotated[str, GitCommitText]

    @classmethod
    def listed(cls, row: str) -> GitReference:
        name, commit, upstream, remote, remote_ref = row.split("\0")
        sha = GitCommitText.decode(commit)
        if name.startswith(BranchUpstream.prefix):
            return LocalGitReference(
                name, sha, BranchUpstream.from_git(name, upstream, remote, remote_ref),
            )
        return cls(name, sha)

    def upstreams(self) -> tuple[BranchUpstream, ...]:
        return ()


@dataclass(frozen=True)
class LocalGitReference(GitReference):
    upstream: BranchUpstream

    def upstreams(self) -> tuple[BranchUpstream, ...]:
        return (self.upstream,)


@dataclass(frozen=True)
class GitHead(ABC):
    """A worktree's own HEAD, decoded independently of the repository's shared refs."""

    checkout: Checkout
    reference: ClassVar[str] = "HEAD"

    @classmethod
    def listed(cls, record: str) -> GitHead:
        # Git's porcelain record opens with "worktree PATH" and "HEAD SHA", in that order;
        # the attribute lines after them declare the branch or the detached state.
        worktree, head, *attributes = record.split("\0")
        path = worktree.removeprefix("worktree ")
        sha = head.removeprefix(f"{cls.reference} ")
        symbolic = next((line.removeprefix("branch ") for line in attributes
                         if line.startswith("branch ")), "")
        checkout = Checkout(Path(path).resolve())
        commit = GitCommitText.decode(sha)
        if not commit.strip("0"):
            return UnbornGitHead(checkout, symbolic)
        if symbolic:
            return AttachedGitHead(checkout, sha=commit, symbolic=symbolic)
        if "detached" not in attributes:
            raise ValueError(f"Git HEAD in {path} has neither a branch nor a detached declaration")
        return DetachedGitHead(checkout, commit)

    @property
    @abstractmethod
    def branch(self) -> str: ...

    @abstractmethod
    def revision(self) -> str: ...

    def required(self) -> tuple[str, ...]:
        return ()

    def checked_branch(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class SymbolicGitHead(GitHead):
    symbolic: str

    @property
    def branch(self) -> str:
        return self.symbolic.removeprefix("refs/heads/")


@dataclass(frozen=True)
class UnbornGitHead(SymbolicGitHead):
    def revision(self) -> str:
        raise UnavailableCommitError(self.checkout.path, (self.reference,))


@dataclass(frozen=True)
class CommittedGitHead(GitHead):
    sha: Annotated[str, GitCommitText]

    def revision(self) -> str:
        return self.sha

    def required(self) -> tuple[str, ...]:
        return (self.sha,)


@dataclass(frozen=True)
class AttachedGitHead(CommittedGitHead, SymbolicGitHead):
    def checked_branch(self) -> tuple[str, ...]:
        return (self.branch,)


@dataclass(frozen=True)
class DetachedGitHead(CommittedGitHead):
    @property
    def branch(self) -> str:
        return self.reference


class RepositoryReads:
    """A pass's repository reads, keyed by Git's common directory, not worktree aliases."""

    def __init__(self) -> None:
        self._histories: dict[Path, CheckoutHistory] = {}
        self._worktree_histories: dict[Path, CheckoutHistory] = {}
        self._remote_cuts: dict[Path, dict[str, dict[str, str]]] = {}

    @classmethod
    def retaining(cls, previous: RepositoryReads) -> RepositoryReads:
        """Carry forward commit histories whose ref snapshot hasn't changed.

        One git-for-each-ref per repository (~1 ms) guards the graph walk
        it protects (50+ ms per repo on every heartbeat tick).
        """
        retained: dict[Path, CheckoutHistory] = {}
        for common_dir, history in previous._histories.items():
            if 'graph' not in history.__dict__:
                continue
            try:
                fresh = history.checkout.read()
                if fresh.refs == history.refs:
                    retained[common_dir] = history
                else:
                    retained[common_dir] = fresh
            except Exception:
                pass
        reads = cls()
        reads._histories = retained
        return reads

    def remote_cuts(self, checkout: Checkout) -> dict[str, dict[str, str]]:
        """Each remote's published refs: worktrees of one repository share their remotes. The
        common directory is the one this pass's cut resolved, never asked of Git again."""
        return self._remote_cuts.setdefault(self.history_for(checkout).checkout.common_dir, {})

    def repository(self, path: Path) -> Repo:
        """The common directory's identity, acquired once within this pass."""
        return self.history_for(Checkout(path)).repository

    def history_for(self, checkout: Checkout) -> CheckoutHistory:
        """One cut per common directory, shared by every worktree it listed. A worktree added
        after that cut is in none of its listings, so it reads a cut of its own: on 2026-10-30
        every new order's worktree (claude-nra--P1067) killed the heartbeat pass that saw it
        mid-read, because head_for found no HEAD for it in the stale listing."""
        checkout = checkout.resolved()
        if checkout.path not in self._worktree_histories:
            common = checkout.common_dir
            if common not in self._histories:
                self._histories[common] = checkout.read()
            history = self._histories[common]
            self._worktree_histories.update((path, history) for path in history.heads)
            if checkout.path not in history.heads:
                history = checkout.read()
            self._worktree_histories[checkout.path] = history
        return self._worktree_histories[checkout.path]


@dataclass(frozen=True)
class GitCommit:
    """The commit facts needed by ancestry and order progress, decoded once."""

    sha: Annotated[str, GitCommitText]
    parents: tuple[Annotated[str, GitCommitText], ...]
    at: float
    message: str


@dataclass(frozen=True)
class CommitSelection:
    """One revision walk, independent of the problem-message filter applied to it."""

    refs: tuple[str, ...]
    excluded: tuple[str, ...]
    first_parent: bool = False


class CommitMessages:
    """Every captured message in one lowered text, so naming a token is one search of it.

    A regex per message cost 137 ms per order on 34,816 commits, in Python, every heartbeat
    pass; one `find` over this text costs about 2.5 ms and the text is built once per pass."""

    word: ClassVar[frozenset[str]] = frozenset(string.ascii_lowercase + string.digits + "_")

    def __init__(self, commits: tuple[GitCommit, ...]) -> None:
        lowered = tuple(commit.message.lower() for commit in commits)
        self.text = "\n".join(lowered)
        self.shas = tuple(commit.sha for commit in commits)
        self.starts = tuple(accumulate((len(message) + 1 for message in lowered), initial=0))
        self._named: dict[str, frozenset[str]] = {}

    def naming(self, token: str) -> frozenset[str]:
        """The commits whose message has this token on its own, in any case."""
        if token not in self._named:
            self._named[token] = frozenset(self._occurrences(token.lower()))
        return self._named[token]

    def _occurrences(self, token: str) -> Iterator[str]:
        at = self.text.find(token)
        while at >= 0:
            end = at + len(token)
            if self.text[max(at - 1, 0):at] in self.word or self.text[end:end + 1] in self.word:
                at = self.text.find(token, at + 1)
                continue
            message = bisect_right(self.starts, at) - 1
            yield self.shas[message]
            at = self.text.find(token, self.starts[message + 1])


class CommitFilter(ABC):
    """Which commits of a captured newest-first walk a reader wants."""

    @abstractmethod
    def commits(self, history: CheckoutHistory, walk: tuple[GitCommit, ...]) -> Iterator[GitCommit]: ...


class EveryCommit(CommitFilter):
    def commits(self, history: CheckoutHistory, walk: tuple[GitCommit, ...]) -> Iterator[GitCommit]:
        return iter(walk)


@dataclass(frozen=True)
class CommitsNaming(CommitFilter):
    """The commits that name a token, such as a problem's ID."""

    token: str

    def commits(self, history: CheckoutHistory, walk: tuple[GitCommit, ...]) -> Iterator[GitCommit]:
        naming = history.messages.naming(self.token)
        return (commit for commit in walk if commit.sha in naming)


class CheckoutHistory:
    """A pass's selected refs and immutable commit graph, never a command-output cache.

    Acquisitions declare additional heads before the graph is first consumed. A later
    acquisition cannot silently change an answer already observed in this cut.
    """

    def __init__(self, checkout: Checkout) -> None:
        self.checkout = checkout
        self.required: set[str] = set()
        self._ancestry: dict[frozenset[str], int] = {}
        self._remote_selections: dict[str, int] = {}
        self._bases: dict[int, tuple[str, ...]] = {}
        self._walks: dict[CommitSelection, tuple[GitCommit, ...]] = {}

    @cached_property
    def repository(self) -> Repo:
        """Origin and source identity shared by every alias of this repository."""
        return Repo.of(self.checkout.path)

    @cached_property
    def refs(self) -> dict[str, GitReference]:
        return {reference.name: reference for reference in map(GitReference.listed, Repo.git(
            self.checkout.common_dir, "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(upstream)"
            "%00%(upstream:remotename)%00%(upstream:remoteref)",
            "refs/heads/", "refs/remotes/",
        ).splitlines())}

    @cached_property
    def heads(self) -> dict[Path, GitHead]:
        """One listing supplies every linked worktree's distinct HEAD before the graph cut."""
        listed = Repo.git(self.checkout.common_dir, "worktree", "list", "--porcelain", "-z")
        return {head.checkout.path: head for head in (
            GitHead.listed(record) for record in listed.split("\0\0")
            if record and "bare" not in record.split("\0"))}

    def require_read(self) -> dict[str, GitReference]:
        """Read the ref cut, propagating unreadable-checkout errors to its caller."""
        return self.refs

    def head_for(self, checkout: Checkout) -> GitHead:
        """Select by the worktree identity; shared refs cannot answer its HEAD."""
        return self.heads[checkout.resolved().path]

    def include(self, *heads: str) -> None:
        """Prepare acquired objects before observation; an already read graph stays immutable."""
        self.required.update(heads)

    @cached_property
    def graph(self) -> dict[str, GitCommit]:
        heads = tuple(dict.fromkeys((
            *self.required, *(reference.commit for reference in self.refs.values()),
            *(sha for head in self.heads.values() for sha in head.required()),
        )))
        if not heads:
            return {}
        text = Repo.git(self.checkout.common_dir, "rev-list", "--stdin", "--no-commit-header",
                        "--format=%H%x00%P%x00%ct%x00%B%x00",
                        input="".join(f"{head}\n" for head in heads))
        commits = {}
        for row in text.split("\0\n"):
            if not row:
                continue
            sha, parents, at, message = row.split("\0", 3)
            commit = GitCommit(
                GitCommitText.decode(sha),
                tuple(map(GitCommitText.decode, parents.split())), float(at), message,
            )
            commits[commit.sha] = commit
        return commits

    def resolve(self, revision: str) -> str:
        """Resolve captured ref names or an unambiguous captured commit abbreviation."""
        revision = revision.removesuffix("^{commit}")
        for name in (revision, f"refs/heads/{revision}", f"refs/remotes/{revision}"):
            if name in self.refs:
                return self.refs[name].commit
        if revision in self.graph:
            return revision
        matches = tuple(sha for sha in self.graph if sha.startswith(revision))
        if len(matches) != 1:
            raise UnavailableCommitError(self.checkout.path, (revision,))
        return matches[0]

    def commit(self, revision: str) -> GitCommit:
        return self.graph[self.resolve(revision)]

    def references(self, prefix: str) -> tuple[str, ...]:
        return tuple(name for name in self.refs if name.startswith(prefix))

    @cached_property
    def positions(self) -> dict[str, int]:
        return {sha: position for position, sha in enumerate(self.graph)}

    @cached_property
    def records(self) -> tuple[GitCommit, ...]:
        return tuple(self.graph.values())

    def bit(self, sha: str) -> int:
        return 1 << self.positions[sha]

    def selected(self, bits: int) -> Iterator[GitCommit]:
        while bits:
            first = bits & -bits
            yield self.records[first.bit_length() - 1]
            bits ^= first

    def ancestors(self, *revisions: str) -> int:
        """Memoized compact reachability; shallow boundaries are graph leaves."""
        selection = frozenset(self.resolve(revision) for revision in revisions)
        if selection not in self._ancestry:
            pending = list(selection)
            found = 0
            while pending:
                sha = pending.pop()
                bit = self.bit(sha)
                if found & bit:
                    continue
                known = self._ancestry.get(frozenset((sha,)), 0)
                if known:
                    found |= known
                    continue
                found |= bit
                pending.extend(parent for parent in self.graph[sha].parents
                               if parent in self.graph)
            self._ancestry[selection] = found
        return self._ancestry[selection]

    def contains(self, ancestor: str, revision: str) -> bool:
        return bool(self.ancestors(revision) & self.bit(self.resolve(ancestor)))

    def unpublished(self, head: str, tracking: str = "") -> int:
        """The whole remote selection has one closure, not one DFS for each remote ref."""
        if tracking not in self._remote_selections:
            self._remote_selections[tracking] = self.ancestors(*(
                ref for ref in self.references("refs/remotes/") if ref != tracking))
        return (self.ancestors(head) & ~self._remote_selections[tracking]).bit_count()

    def walk(self, selection: CommitSelection) -> tuple[GitCommit, ...]:
        """Traverse each selection once; problem filters share the resulting commit order."""
        if selection not in self._walks:
            seen = self.ancestors(*selection.excluded)
            pending: list[tuple[float, int, str]] = []
            sequence = 0
            for ref in selection.refs:
                commit = self.commit(ref)
                heapq.heappush(pending, (-commit.at, sequence, commit.sha))
                sequence += 1
            commits: list[GitCommit] = []
            while pending:
                _at, _sequence, sha = heapq.heappop(pending)
                bit = self.bit(sha)
                if seen & bit:
                    continue
                seen |= bit
                commit = self.graph[sha]
                commits.append(commit)
                parents = commit.parents[:1] if selection.first_parent else commit.parents
                for parent in parents:
                    if parent in self.graph and not seen & self.bit(parent):
                        heapq.heappush(pending, (-self.graph[parent].at, sequence, parent))
                        sequence += 1
            self._walks[selection] = tuple(commits)
        return self._walks[selection]

    @cached_property
    def messages(self) -> CommitMessages:
        return CommitMessages(tuple(self.graph.values()))

    def commits(self, refs: tuple[str, ...], *excluded: str, selected: CommitFilter = EveryCommit(),
                first_parent: bool = False) -> Iterator[GitCommit]:
        """Filter a captured newest-first walk without traversing the graph again."""
        return selected.commits(self, self.walk(CommitSelection(refs, excluded, first_parent)))

    @cached_property
    def children(self) -> dict[str, tuple[str, ...]]:
        """Each captured commit's children, inverted once from the parent edges."""
        children: dict[str, list[str]] = {sha: [] for sha in self.graph}
        for commit in self.records:
            for parent in commit.parents:
                if parent in children:
                    children[parent].append(commit.sha)
        return {sha: tuple(shas) for sha, shas in children.items()}

    def merge_base(self, left: str, right: str) -> tuple[str, ...]:
        """Best common ancestors: common commits with no child in common.

        The common set is ancestor-closed, so a best base is either a tip or the parent of a
        commit only one side reaches; only those few candidates are tested, never the whole
        shared history.
        """
        tips = (self.resolve(left), self.resolve(right))
        common = self.ancestors(tips[0]) & self.ancestors(tips[1])
        if common not in self._bases:
            candidates = dict.fromkeys((*tips, *(
                parent for commit in self.selected(
                    self.ancestors(tips[0]) ^ self.ancestors(tips[1]))
                for parent in commit.parents if parent in self.graph)))
            best = tuple(self.graph[sha] for sha in candidates
                         if common & self.bit(sha)
                         and not any(common & self.bit(child) for child in self.children[sha]))
            self._bases[common] = tuple(commit.sha for commit in sorted(
                best, key=lambda commit: commit.at, reverse=True))
        return self._bases[common]

    def count(self, head: str, *excluded: str) -> int:
        return (self.ancestors(head) & ~self.ancestors(*excluded)).bit_count()


@dataclass(frozen=True)
class SubmoduleTransfer:
    """An initialized module's own Git directory and its original worktree setting."""

    store: Path
    relative: Path
    original_worktree: str

    @classmethod
    def capture(cls, module: PinnedSubmodule, source: Path, destination: Path) -> Self:
        store = module.store.resolve(strict=True)
        if store.is_relative_to(source):
            raise ValueError(f'Submodule Git directory {store} must be outside the moved tree')
        relative = module.path.relative_to(source)
        # Sibling relocation keeps .git's relative indirection at the same depth.
        linked = (module.path / '.git').read_text().strip().removeprefix('gitdir: ')
        if (destination / relative / linked).resolve() != store:
            raise ValueError(f'Submodule {module.path} has an unsafe Git directory link')
        original = Repo.git(module.path, 'config', '--local', '--get', 'core.worktree').strip()
        return cls(store, relative, original)

    def apply(self, destination: Path) -> None:
        Repo.git(destination, '--git-dir', str(self.store),
                 '--work-tree', str(destination / self.relative), 'config', '--local',
                 'core.worktree', str(destination / self.relative))

    def restore(self, source: Path) -> None:
        Repo.git(source, '--git-dir', str(self.store),
                 '--work-tree', str(source / self.relative), 'config', '--local',
                 'core.worktree', self.original_worktree)


@dataclass(frozen=True, kw_only=True)
class CheckoutMove:
    """One checkout head moved from a source to a destination."""

    source: Path
    destination: Path
    head: str


@dataclass(frozen=True, kw_only=True)
class CheckoutTransfer(CheckoutMove):
    """The retained Git identity and the paths of one move."""

    branch: str

    def restore(self, common: Path, modules: tuple[SubmoduleTransfer, ...]) -> None:
        self.destination.rename(self.source)
        Repo.git(common, 'worktree', 'repair', str(self.source))
        for module in modules:
            module.restore(self.source)


class LaunchCheckout(Checkout):
    """A project qualified for a new launch, not a historical filesystem promise."""

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls.checked(FieldCodec.decode(str, data))


class ScratchCheckout(Checkout):
    """A tree made for one run and discarded with whatever the run left in it."""

    def held_work(self) -> str:
        return ""
