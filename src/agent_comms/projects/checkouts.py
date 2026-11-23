from __future__ import annotations

import base64
import heapq
import os
import re
import shutil
import string
from abc import (
    ABC,
    abstractmethod,
)
from bisect import bisect_left, bisect_right
from collections.abc import (
    Callable,
    Iterator,
    Mapping,
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
from types import MappingProxyType
from typing import (
    Annotated,
    ClassVar,
    Self,
)

from immutables import Map, MapMutation

from agent_comms.errors import MissingSourceLine
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    DeclaredWireType,
    FieldCodec,
    GitCommitText,
    JsonValue,
    PathText,
    PresentMember,
    SingleFieldValue,
)
from agent_comms.projects.git_errors import GitCommandError, UnavailableCommitError
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_environment import RepositoryEnvironment
from agent_comms.storage.file_observation import DocumentBytes, FileObservation, FileRevision
from agent_comms.storage.native_changes import (
    ChangedSources,
    DirectorySource,
    FileSource,
    SourceChanges,
)


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
        """All worktrees use the Git repository's one test prefix."""
        return RepositoryEnvironment.of(self.path).prefix

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
        return CheckoutHistory.capture(self)

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
    def read(cls, repository: Path, *prefixes: str) -> dict[str, GitReference]:
        """Capture Git's refs and decode their declared upstreams once."""
        return {reference.name: reference for reference in map(cls.listed, Repo.git(
            repository, "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(upstream)"
            "%00%(upstream:remotename)%00%(upstream:remoteref)",
            *prefixes,
        ).splitlines())}

    @classmethod
    def listed(cls, row: str) -> GitReference:
        name, commit, upstream, remote, remote_ref = row.split("\0")
        sha = GitCommitText.from_text(commit)
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
        commit = GitCommitText.from_text(sha)
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


@dataclass
class CheckoutStatus:
    """One exact worktree-status cut, retained until one of its native sources changes.

    Git owns path classification.  The filesystem owns edit clocks.  The native
    registration keeps that joined answer valid without rebuilding every pathname
    and restatting every dirty file on every heartbeat.
    """

    checkout: Checkout
    head: GitHead
    paths: tuple[Path, ...]
    modified: tuple[tuple[Path, float], ...]
    directories: tuple[Path, ...]
    git_sources: tuple[Path, ...]
    changes: ChangedSources

    @property
    def edits(self) -> tuple[float, ...]:
        newest = max((modified for _path, modified in self.modified), default=None)
        return () if newest is None else (newest,)

    @staticmethod
    def _excluded_directories(checkout: Checkout) -> frozenset[Path]:
        ignored = Repo.git_bytes(
            checkout.path,
            "--no-optional-locks",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        )
        gitlinks = Repo.git_bytes(checkout.path, "ls-files", "--stage", "-z")
        return frozenset((
            Path(os.fsdecode(name[:-1]))
            for name in ignored.split(b"\0")
            if name.endswith(b"/")
        )).union(
            Path(os.fsdecode(entry.split(b"\t", 1)[1]))
            for entry in gitlinks.split(b"\0")
            if entry.startswith(b"160000 ")
        )

    @classmethod
    def _directories(cls, checkout: Checkout) -> tuple[Path, ...]:
        excluded = cls._excluded_directories(checkout)
        directories: list[Path] = []
        for root, names, _files in os.walk(checkout.path):
            root_path = Path(root)
            relative = root_path.relative_to(checkout.path)
            names[:] = [
                name
                for name in names
                if name != ".git" and relative / name not in excluded
            ]
            directories.append(root_path)
        return tuple(directories)

    @staticmethod
    def _git_sources(checkout: Checkout) -> tuple[Path, ...]:
        common = checkout.common_dir
        paths = [
            checkout.path / ".git",
            Path(Repo.git(
                checkout.path, "rev-parse", "--path-format=absolute", "--git-path", "HEAD",
            ).strip()),
            Path(Repo.git(
                checkout.path, "rev-parse", "--path-format=absolute", "--git-path", "index",
            ).strip()),
            Path(Repo.git(
                checkout.path,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "config.worktree",
            ).strip()),
            common / "config",
            common / "info" / "exclude",
            *Repo.configuration_files(checkout.path),
        ]
        sparse = Path(
            Repo.git(
                checkout.path,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "info/sparse-checkout",
            ).strip()
        )
        paths.append(sparse)
        excludes = Repo._run(
            checkout.path, "config", "--path", "--get", "core.excludesfile",
        )
        if excludes.returncode not in (0, 1):
            Repo._output(checkout.path, excludes)
        configured = excludes.stdout.decode().strip()
        if configured:
            paths.append(Path(configured).expanduser().resolve())
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _status(
        checkout: Checkout, paths: tuple[Path, ...] = (),
    ) -> tuple[tuple[Path, ...], tuple[tuple[Path, float], ...]]:
        pathspec = (() if not paths else (
            "--", *(os.fspath(path.relative_to(checkout.path)) for path in paths),
        ))
        fields = iter(
            Repo.git_bytes(
                checkout.path,
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=all",
                *pathspec,
            ).split(b"\0")
        )
        names: list[str] = []
        for entry in fields:
            if entry:
                names.append(os.fsdecode(entry[3:]))
                if {b"R", b"C"} & {entry[:1], entry[1:2]}:
                    names.append(os.fsdecode(next(fields)))
        status_paths = tuple(sorted(
            dict.fromkeys(checkout.path / name for name in names), key=os.fspath,
        ))
        modified: list[tuple[Path, float]] = []
        descriptor = os.open(checkout.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for path in status_paths:
                try:
                    timestamp = os.stat(
                        path.relative_to(checkout.path), dir_fd=descriptor,
                    ).st_mtime
                except (FileNotFoundError, NotADirectoryError):
                    continue
                modified.append((path, timestamp))
        finally:
            os.close(descriptor)
        return status_paths, tuple(modified)

    @classmethod
    def capture(cls, checkout: Checkout, head: GitHead) -> Self:
        checkout = checkout.resolved()
        for _attempt in range(ChangedSources.reconcile_attempts):
            directories = cls._directories(checkout)
            git_sources = cls._git_sources(checkout)
            changes = ChangedSources(
                *(DirectorySource(path) for path in directories),
                *(FileSource(path) for path in git_sources),
            )
            try:
                paths, modified = cls._status(checkout)
                if (
                    directories == cls._directories(checkout)
                    and git_sources == cls._git_sources(checkout)
                    and not changes.changed_paths()
                ):
                    return cls(
                        checkout, head, paths, modified, directories, git_sources, changes,
                    )
            except BaseException:
                changes.close()
                raise
            changes.close()
        raise OSError("Worktree sources did not stabilize while reading status")

    def following(self, head: GitHead) -> Self | None:
        """Advance an exact worktree-status cut from complete native path events.

        Git metadata, directory topology and lost events still reacquire the
        complete answer. Ordinary file events ask Git only for those paths and
        reuse this registration after proving no event raced the projection.
        """
        if self.head != head:
            return None
        events: set[Path] = set()
        for _attempt in range(ChangedSources.reconcile_attempts):
            changed = self.changes.changes()
            if not changed.declared:
                return self if not events else self._following_paths(tuple(events))
            following = self.incremental_paths(changed)
            if following is None:
                return None
            events.update(following)
            projected = self._following_paths(tuple(events))
            raced = self.changes.changes()
            if not raced.declared:
                return projected
            following = self.incremental_paths(raced)
            if following is None:
                return None
            events.update(following)
        return None

    def incremental_paths(self, changed: SourceChanges) -> frozenset[Path] | None:
        """Exact ordinary-file events whose Git classification is path-local."""
        if (
            not changed.complete
            or not changed.events
            or changed.declared.intersection(self.git_sources)
            or any(
                event in self.directories
                or event.is_dir()
                or event.name == ".gitignore"
                for event in changed.events
            )
        ):
            return None
        return changed.events

    def _following_paths(self, events: tuple[Path, ...]) -> Self:
        selected = tuple(sorted(
            (event for event in events if event.is_relative_to(self.checkout.path)),
            key=os.fspath,
        ))
        if len(selected) != len(events):
            raise ValueError("Worktree event lies outside its checkout")
        paths, modified = self._status(self.checkout, selected)
        affected = set(selected).union(paths)
        retained_paths = set(self.paths).difference(selected)
        retained_modified = dict(self.modified)
        for path in affected:
            retained_modified.pop(path, None)
        retained_modified.update(modified)
        return type(self)(
            self.checkout,
            self.head,
            tuple(sorted(retained_paths.union(paths), key=os.fspath)),
            tuple(sorted(retained_modified.items(), key=lambda item: os.fspath(item[0]))),
            self.directories,
            self.git_sources,
            self.changes,
        )

    def close(self) -> None:
        self.changes.close()


@dataclass(frozen=True)
class GitSourceRevision:
    """One captured set of filesystem sources that must remain a single Git cut."""

    sources: tuple[tuple[Path, FileObservation], ...]

    @staticmethod
    def _tree_paths(root: Path) -> tuple[Path, ...]:
        if not root.is_dir():
            return (root,)
        paths: list[Path] = [root]
        for directory, names, files in os.walk(root):
            parent = Path(directory)
            paths.extend(parent / name for name in names)
            paths.extend(parent / name for name in files)
        return tuple(paths)

    @classmethod
    def source_paths(cls, checkout: Checkout) -> tuple[Path, ...]:
        raise NotImplementedError

    @classmethod
    def capture(cls, checkout: Checkout) -> Self:
        return cls(tuple(
            (path, FileRevision.capture(path))
            for path in cls.source_paths(checkout)
        ))

    def current(self) -> bool:
        return all(FileRevision.capture(path) == observed
                   for path, observed in self.sources)


@dataclass(frozen=True)
class GitGraphRevision(GitSourceRevision):
    """The replacement and boundary sources a lazy graph command still reads."""

    @classmethod
    def source_paths(cls, checkout: Checkout) -> tuple[Path, ...]:
        common = checkout.common_dir
        return tuple(dict.fromkeys((
            *cls._tree_paths(common / "refs" / "replace"),
            common / "packed-refs",
            common / "shallow",
            common / "info" / "grafts",
            Path(Repo.git(
                checkout.path,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "config.worktree",
            ).strip()),
            *Repo.configuration_files(checkout.path),
        )))


@dataclass(frozen=True)
class GitHistoryRevision(GitSourceRevision):
    """The filesystem sources of one Git refs, worktree-head and walk-policy cut."""

    graph: GitGraphRevision
    ref_sources: tuple[tuple[Path, FileObservation], ...]
    head_sources: tuple[tuple[Path, FileObservation], ...]

    @classmethod
    def ref_paths(cls, checkout: Checkout) -> tuple[Path, ...]:
        common = checkout.common_dir
        return tuple(dict.fromkeys((
            *cls._tree_paths(common / "refs"),
            common / "packed-refs",
        )))

    @staticmethod
    def _worktree_head_paths(worktrees: Path) -> tuple[Path, ...]:
        """Watch only the files ``git worktree list`` reads, never submodule trees or logs."""
        if not worktrees.is_dir():
            return (worktrees,)
        paths: list[Path] = [worktrees]
        with os.scandir(worktrees) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.is_dir(follow_symlinks=False):
                    directory = Path(entry.path)
                    paths.extend((directory, directory / "HEAD", directory / "gitdir"))
        return tuple(paths)

    @classmethod
    def head_paths(cls, checkout: Checkout) -> tuple[Path, ...]:
        common = checkout.common_dir
        return tuple(dict.fromkeys((
            *cls._worktree_head_paths(common / "worktrees"),
            common / "HEAD",
        )))

    @classmethod
    def source_paths(cls, checkout: Checkout) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((
            *cls.ref_paths(checkout),
            *cls.head_paths(checkout),
            *GitGraphRevision.source_paths(checkout),
        )))

    @classmethod
    def capture(cls, checkout: Checkout, previous: Self | None = None) -> Self:
        """Refresh refs, worktree heads and graph policy as separate source families.

        A linked worktree HEAD moves whenever an agent commits.  That does not change Git's
        configuration topology or replacement policy, so rediscovering that family with Git
        commands on every heartbeat rebuilt an unchanged source.  Reuse the exact graph cut
        while it remains current; refs and worktree heads still recapture completely.
        """
        graph = (
            previous.graph
            if previous is not None and previous.graph.current()
            else GitGraphRevision.capture(checkout)
        )
        ref_sources = tuple(
            (path, FileRevision.capture(path)) for path in cls.ref_paths(checkout)
        )
        head_sources = tuple(
            (path, FileRevision.capture(path)) for path in cls.head_paths(checkout)
        )
        observed = dict((*graph.sources, *ref_sources, *head_sources))
        return cls(
            tuple((path, observed[path]) for path in dict.fromkeys(observed)),
            graph,
            ref_sources,
            head_sources,
        )

    def same_refs(self, other: Self) -> bool:
        # Local branch upstreams are configuration-owned fields of GitReference.
        return self.ref_sources == other.ref_sources and self.graph == other.graph

    def same_heads(self, other: Self) -> bool:
        # An attached worktree's reported commit moves with its branch ref.
        return self.ref_sources == other.ref_sources and self.head_sources == other.head_sources

    def graph_revision(self, checkout: Checkout) -> GitGraphRevision:
        """Select the exact captured files a lazy graph command can still consult."""
        return self.graph


class RepositoryReads:
    """A pass's repository reads, keyed by Git's common directory, not worktree aliases."""

    def __init__(self) -> None:
        self._histories: dict[Path, CheckoutHistory] = {}
        self._worktree_histories: dict[Path, CheckoutHistory] = {}
        self._remote_cuts: dict[Path, dict[str, dict[str, str]]] = {}
        self._statuses: dict[Path, CheckoutStatus] = {}

    @classmethod
    def retaining(cls, previous: RepositoryReads) -> Self:
        """Retain a repository cut while refs, every worktree HEAD and walk policy agree.

        Packed refs alone cannot observe loose refs, detached HEADs or worktree births.
        A cut owns refs, heads and policy before any consumer happens to ask for its graph;
        graph consumption is not the identity of that source.  Changed cuts keep only the
        derived graph/message work whose own acquisition contracts allow it.
        """
        retained: dict[Path, CheckoutHistory] = {}
        successors: dict[int, CheckoutHistory] = {}
        for common_dir, history in previous._histories.items():
            if history.source_revision is None:
                continue
            try:
                successor = (
                    history
                    if history.sources_current()
                    else history.following(CheckoutHistory.capture(history.checkout, history))
                )
                retained[common_dir] = successor
                successors[id(history)] = successor
            except (GitCommandError, OSError):
                # A later requested acquisition owns reporting its unreadable source.
                continue
        reads = cls()
        reads._histories = retained
        reads._worktree_histories = {
            path: successor
            for path, history in previous._worktree_histories.items()
            if (successor := successors.get(id(history))) is not None
            and path in successor.heads
        }
        reads._statuses = previous._statuses
        previous._statuses = {}
        return reads

    def status_for(self, checkout: Checkout, head: GitHead) -> CheckoutStatus:
        checkout = checkout.resolved()
        retained = self._statuses.pop(checkout.path, None)
        if retained is not None:
            try:
                following = retained.following(head)
                if following is not None:
                    self._statuses[checkout.path] = following
                    return following
            except BaseException:
                retained.close()
                raise
            retained.close()
        captured = CheckoutStatus.capture(checkout, head)
        self._statuses[checkout.path] = captured
        return captured

    def close(self) -> None:
        statuses, self._statuses = self._statuses, {}
        for status in statuses.values():
            status.close()

    def __del__(self) -> None:
        if hasattr(self, "_statuses"):
            self.close()

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
                try:
                    self._histories[common] = CheckoutHistory.capture(checkout)
                except (GitCommandError, OSError):
                    # Keep acquisition lazy so the requested answer reports its own
                    # unreadable ref, policy or boundary source, as before retention.
                    self._histories[common] = CheckoutHistory(checkout)
            history = self._histories[common]
            self._worktree_histories.update((path, history) for path in history.heads)
            if checkout.path not in history.heads:
                try:
                    history = CheckoutHistory.capture(checkout)
                except (GitCommandError, OSError):
                    history = CheckoutHistory(checkout)
            self._worktree_histories[checkout.path] = history
        return self._worktree_histories[checkout.path]


@dataclass(frozen=True)
class GitCommit:
    """The commit facts needed by ancestry and order progress, decoded once."""

    sha: Annotated[str, GitCommitText]
    parents: tuple[Annotated[str, GitCommitText], ...]
    at: float
    message: str

    @classmethod
    def listed(cls, row: str) -> GitCommit:
        sha, parents, at, message = row.split("\0", 3)
        return cls(GitCommitText.from_text(sha),
                   tuple(map(GitCommitText.from_text, parents.split())), float(at), message)

    @classmethod
    def read(cls, source: GitGraphSource, heads: tuple[str, ...],
             *walk_options: str) -> dict[str, GitCommit]:
        """Git supplies these complete commit facts, in its own selected walk order."""
        if not heads:
            return {}
        text = source.git(
            "rev-list",
            "--stdin",
            "--no-commit-header",
            "--format=%H%x00%P%x00%ct%x00%B%x00",
            *walk_options,
            input="".join(f"{head}\n" for head in heads),
        )
        return {commit.sha: commit for commit in (
            cls.listed(row) for row in text.split("\0\n") if row)}


@dataclass(frozen=True)
class CommitSelection:
    """One revision walk, independent of the problem-message filter applied to it."""

    refs: tuple[str, ...]
    excluded: tuple[str, ...]
    first_parent: bool = False


class CommitMessages:
    """One captured message index shared by all problem lookups in this graph.

    ASCII words have exact postings. Literal phrases use the same captured text and
    boundary rule; neither reader asks Git again or changes the captured messages.
    """

    word: ClassVar[frozenset[str]] = frozenset(string.ascii_lowercase + string.digits + "_")
    words: ClassVar[re.Pattern[str]] = re.compile(f"[{''.join(sorted(word))}]+")

    def __init__(self, commits: tuple[GitCommit, ...]) -> None:
        self.lowered = tuple(commit.message.lower() for commit in commits)
        self.shas = tuple(commit.sha for commit in commits)
        self._named: dict[str, frozenset[str]] = {}

    @cached_property
    def text(self) -> str:
        return "\n".join(self.lowered)

    @cached_property
    def starts(self) -> tuple[int, ...]:
        return tuple(accumulate((len(message) + 1 for message in self.lowered), initial=0))

    @cached_property
    def word_names(self) -> Map[str, Map[str, None]]:
        """One cold build groups postings before freezing their immutable roots."""
        names: dict[str, set[str]] = {}
        for sha, message in zip(self.shas, self.lowered):
            for word in set(self.words.findall(message)):
                if word not in names:
                    names[word] = set()
                names[word].add(sha)
        return Map((word, Map(dict.fromkeys(shas))) for word, shas in names.items())

    @cached_property
    def messages_by_sha(self) -> Map[str, str]:
        return Map(zip(self.shas, self.lowered))

    def acquired_index(self) -> CommitMessages:
        """Only an index actually read by the old cut is carried into a fresh cut."""
        return self if CommitMessages.word_names.attrname in self.__dict__ else NoCommitMessages()

    def cache_postings(
        self, positions: Mapping[str, int],
    ) -> tuple[CommitWordPosting, ...]:
        """Encode the acquired index in the cache's positional representation."""
        if CommitMessages.word_names.attrname in self.__dict__:
            return tuple(
                CommitWordPosting(
                    word,
                    tuple(sorted(positions[sha] for sha in posting if sha in positions)),
                )
                for word, posting in sorted(self.word_names.items())
            )
        postings: dict[str, set[int]] = {}
        for sha, message in zip(self.shas, self.lowered):
            position = positions.get(sha)
            if position is None:
                continue
            for word in set(self.words.findall(message)):
                postings.setdefault(word, set()).add(position)
        return tuple(
            CommitWordPosting(word, tuple(sorted(indexes)))
            for word, indexes in sorted(postings.items())
        )

    @classmethod
    def _updated_postings(
        cls,
        previous: Mapping[str, str],
        current: Mapping[str, str],
        retained: tuple[CommitWordPosting, ...],
        positions: Mapping[str, int],
    ) -> tuple[CommitWordPosting, ...]:
        """Apply only changed commit messages to retained positional postings."""
        postings = {posting.word: posting.positions for posting in retained}
        edits: dict[str, set[int]] = {}
        for sha in set(previous).union(current):
            if previous.get(sha) == current.get(sha):
                continue
            before = set(cls.words.findall(previous[sha])) if sha in previous else set()
            after = set(cls.words.findall(current[sha])) if sha in current else set()
            position = positions.get(sha)
            for word in before ^ after:
                posting = edits.setdefault(word, set(postings.get(word, ())))
                if word in after and position is not None:
                    posting.add(position)
                elif position is not None:
                    posting.discard(position)
        postings.update((word, tuple(sorted(posting))) for word, posting in edits.items())
        return tuple(
            CommitWordPosting(word, tuple(posting))
            for word, posting in sorted(postings.items()) if posting
        )

    def following(self, commits: tuple[GitCommit, ...]) -> CommitMessages:
        """New source messages share immutable postings, never a mutable answer cache."""
        return RetainedCommitMessages(commits, self)

    def naming(self, token: str) -> frozenset[str]:
        """The commits whose message has this literal token on its own, in any case."""
        token = token.lower()
        if token not in self._named:
            self._named[token] = (frozenset(self.word_names.get(token, ()))
                                  if self.words.fullmatch(token)
                                  else frozenset(self._occurrences(token)))
        return self._named[token]

    def _occurrences(self, token: str) -> Iterator[str]:
        at = self.text.find(token)
        while at >= 0:
            end = at + len(token)
            if self.text[max(at - 1, 0):at] in self.word or self.text[end:end + 1] in self.word:
                at = self.text.find(token, at + 1)
                continue
            message = bisect_right(self.starts, at) - 1
            next_start = self.starts[message + 1]
            if end >= next_start:
                at = self.text.find(token, at + 1)
                continue
            yield self.shas[message]
            at = self.text.find(token, next_start)


class NoCommitMessages(CommitMessages):
    """No acquired index: the next messages use the cold builder directly."""

    def __init__(self) -> None:
        super().__init__(())

    def following(self, commits: tuple[GitCommit, ...]) -> CommitMessages:
        return CommitMessages(commits)


class RetainedCommitMessages(CommitMessages):
    """A fresh message cut whose index changes only changed original text."""

    def __init__(self, commits: tuple[GitCommit, ...], earlier: CommitMessages) -> None:
        super().__init__(commits)
        self._earlier = earlier

    @cached_property
    def word_names(self) -> Map[str, Map[str, None]]:
        """Update only changed messages over immutable word and posting roots."""
        previous, current = self._earlier.messages_by_sha, self.messages_by_sha
        changed = {sha for sha in set(previous).union(current)
                   if previous.get(sha) != current.get(sha)}
        edits: dict[str, MapMutation[str, None]] = {}
        for sha in changed:
            before = set(self.words.findall(previous[sha])) if sha in previous else set()
            after = set(self.words.findall(current[sha])) if sha in current else set()
            for word in before ^ after:
                if word not in edits:
                    edits[word] = self._earlier.word_names.get(word, Map()).mutate()
                if word in after:
                    edits[word][sha] = None
                else:
                    del edits[word][sha]
        result = self._earlier.word_names.mutate()
        for word, edited in edits.items():
            posting = edited.finish()
            if posting:
                result[word] = posting
            else:
                del result[word]
        return result.finish()

    def cache_postings(
        self, positions: Mapping[str, int],
    ) -> tuple[CommitWordPosting, ...]:
        """Apply changed messages directly to retained positional postings.

        The cache already owns compact posting positions. Expanding those positions into
        nested immutable SHA maps merely to collapse them back to positions made a one-commit
        worker restart rebuild the entire index.
        """
        return self._updated_postings(
            self._earlier.messages_by_sha,
            self.messages_by_sha,
            self._earlier.cache_postings(positions),
            positions,
        )


@dataclass(frozen=True)
class CommitWalk:
    """One selected commit order and its positions, shared by all message filters."""

    records: tuple[GitCommit, ...]

    def __iter__(self) -> Iterator[GitCommit]:
        return iter(self.records)

    @cached_property
    def positions(self) -> dict[str, int]:
        return {commit.sha: position for position, commit in enumerate(self.records)}

    def naming(self, shas: frozenset[str]) -> Iterator[GitCommit]:
        positions = self.positions
        return (self.records[position] for position in sorted(
            positions[sha] for sha in shas if sha in positions))


class CommitFilter(ABC):
    """Which commits of a captured newest-first walk a reader wants."""

    @abstractmethod
    def commits(self, history: CheckoutHistory, walk: CommitWalk) -> Iterator[GitCommit]: ...


class EveryCommit(CommitFilter):
    def commits(self, history: CheckoutHistory, walk: CommitWalk) -> Iterator[GitCommit]:
        return iter(walk)


@dataclass(frozen=True)
class CommitsNaming(CommitFilter):
    """The commits that name a token, such as a problem's ID."""

    token: str

    def commits(self, history: CheckoutHistory, walk: CommitWalk) -> Iterator[GitCommit]:
        naming = history.messages.naming(self.token)
        return walk.naming(naming)


@dataclass(frozen=True)
class GitHistoryPolicy:
    """The captured Git sources that can change a walk without moving branch tips."""

    replacements: tuple[GitReference, ...]
    replacement_setting: str
    boundary_files: tuple[tuple[Path, DocumentBytes], ...]


@dataclass(frozen=True)
class GitGraphSource:
    """The repository and effective walk policy that supplied immutable commit facts."""

    repository: Path
    policy: GitHistoryPolicy
    revision: GitGraphRevision | None = field(default=None, compare=False, repr=False)

    def require_current(self) -> None:
        """Refuse a live Git query after any source of this captured cut moved."""
        if self.revision is None or not self.revision.current():
            raise OSError("Git history sources changed after their captured cut")

    def git(self, *args: str, input: str = "") -> str:
        """Run a graph query under this cut's captured replacement policy."""
        self.require_current()
        answer = Repo.git(
            self.repository,
            "-c",
            f"core.useReplaceRefs={self.policy.replacement_setting}",
            *args,
            input=input,
        )
        self.require_current()
        return answer


@dataclass(frozen=True)
class GitBoundarySource:
    """One exact policy file cut in the restart-safe Git graph source."""

    path: Annotated[Path, PathText]
    revision: FileObservation
    content: str

    @classmethod
    def capturing(cls, path: Path, document: DocumentBytes) -> GitBoundarySource:
        return cls(path, document.revision, base64.b64encode(document.content).decode("ascii"))

    def document(self) -> DocumentBytes:
        return DocumentBytes(self.revision, base64.b64decode(self.content, validate=True))


@dataclass(frozen=True)
class GitGraphCacheSource:
    """The recoverable Git source stored beside its derived graph."""

    repository: Annotated[Path, PathText]
    replacements: tuple[GitReference, ...]
    replacement_setting: str
    boundaries: tuple[GitBoundarySource, ...]

    @classmethod
    def capturing(cls, source: GitGraphSource) -> GitGraphCacheSource:
        return cls(
            source.repository,
            source.policy.replacements,
            source.policy.replacement_setting,
            tuple(GitBoundarySource.capturing(path, document)
                  for path, document in source.policy.boundary_files),
        )

    def graph_source(self) -> GitGraphSource:
        return GitGraphSource(self.repository, GitHistoryPolicy(
            self.replacements,
            self.replacement_setting,
            tuple((boundary.path, boundary.document()) for boundary in self.boundaries),
        ))


class CommitGraph(Mapping[str, GitCommit]):
    """No acquired graph. Its first cut reads all original facts through GitCommit."""

    def __getitem__(self, sha: str) -> GitCommit:
        raise KeyError(sha)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def following(self, source: GitGraphSource, heads: tuple[str, ...]) -> CapturedCommitGraph:
        return CapturedCommitGraph(source, GitCommit.read(source, heads))


@dataclass(frozen=True, eq=False)
class CapturedCommitGraph(CommitGraph):
    """An immutable source cut; a fresh Git walk still owns reachability and ordering."""

    source: GitGraphSource
    commits: Mapping[str, GitCommit]

    def __post_init__(self) -> None:
        # Capture the supplied map; no caller keeps a writable handle to this cut.
        object.__setattr__(self, "commits", MappingProxyType(dict(self.commits)))

    def __getitem__(self, sha: str) -> GitCommit:
        return self.commits[sha]

    def __iter__(self) -> Iterator[str]:
        return iter(self.commits)

    def __len__(self) -> int:
        return len(self.commits)

    def following(self, source: GitGraphSource, heads: tuple[str, ...]) -> CapturedCommitGraph:
        if source != self.source:
            return super().following(source, heads)
        if not heads:
            return type(self)(source, {})
        order = source.git("rev-list", "--stdin", input="".join(f"{head}\n" for head in heads))
        shas = tuple(map(GitCommitText.from_text, order.splitlines()))
        missing = tuple(sha for sha in shas if sha not in self)
        added = (GitCommit.read(source, missing, "--no-walk=unsorted")
                 if missing else {})
        return type(self)(source, {
            sha: self[sha] if sha in self else added[sha] for sha in shas})


class RestoredCommitMessages(CommitMessages):
    """A persisted index resolves only the posting this process actually asks for."""

    def __init__(
        self, commits: tuple[GitCommit, ...], postings: tuple[CommitWordPosting, ...],
    ) -> None:
        super().__init__(commits)
        self._commits = commits
        self._postings = postings
        self._positions = {posting.word: posting.positions for posting in postings}

    def following(self, commits: tuple[GitCommit, ...]) -> CommitMessages:
        if commits == self._commits:
            return self
        if CommitMessages.word_names.attrname in self.__dict__:
            return super().following(commits)
        positions = {commit.sha: index for index, commit in enumerate(commits)}
        previous = dict(zip(self.shas, self.lowered))
        current = {commit.sha: commit.message.lower() for commit in commits}
        updated = self._updated_postings(
            previous, current, self.cache_postings(positions), positions,
        )
        return RestoredCommitMessages(commits, updated)

    def acquired_index(self) -> CommitMessages:
        return self

    def cache_postings(
        self, positions: Mapping[str, int],
    ) -> tuple[CommitWordPosting, ...]:
        """Remap retained positions without expanding them into SHA maps."""
        return tuple(CommitWordPosting(
            posting.word,
            tuple(sorted(positions[sha] for old in posting.positions
                         if (sha := self.shas[old]) in positions)),
        ) for posting in self._postings)

    def naming(self, token: str) -> frozenset[str]:
        token = token.lower()
        if token not in self._named:
            self._named[token] = (
                frozenset(self.shas[position] for position in self._positions.get(token, ()))
                if self.words.fullmatch(token)
                else frozenset(self._occurrences(token))
            )
        return self._named[token]

    @cached_property
    def word_names(self) -> Map[str, Map[str, None]]:
        return Map(
            (word, Map(dict.fromkeys(self.shas[position] for position in positions)))
            for word, positions in self._positions.items())


@dataclass(frozen=True)
class CommitWordPosting:
    """One derived exact-word posting retained with its commit messages."""

    word: str
    positions: tuple[int, ...]


@dataclass(frozen=True)
class GitHistorySnapshot(DeclaredWireType):
    """A restart-safe derived graph whose complete supplying source remains recoverable."""

    wire_type: ClassVar[object] = JsonValue
    source: GitGraphCacheSource
    heads: tuple[Annotated[str, GitCommitText], ...]
    commits: tuple[GitCommit, ...]
    postings: tuple[CommitWordPosting, ...]

    def __post_init__(self) -> None:
        commits = {commit.sha for commit in self.commits}
        if len(commits) != len(self.commits):
            raise ValueError("Cached Git commits must have distinct identities")
        ordered = tuple(sorted(commits))
        if any(not self.resolves_once(ordered, head) for head in self.heads):
            raise ValueError("Each cached Git revision must resolve to one cached commit")
        words: set[str] = set()
        for posting in self.postings:
            if posting.word in words or not CommitMessages.words.fullmatch(posting.word):
                raise ValueError("Cached Git message postings must have distinct exact words")
            if (tuple(sorted(set(posting.positions))) != posting.positions
                    or any(position < 0 or position >= len(self.commits)
                           for position in posting.positions)):
                raise ValueError("Cached Git message postings must index cached commits")
            words.add(posting.word)

    @staticmethod
    def resolves_once(commits: tuple[str, ...], revision: str) -> bool:
        """Whether one sorted captured commit has this exact Git revision prefix."""
        position = bisect_left(commits, revision)
        return (
            position < len(commits)
            and commits[position].startswith(revision)
            and (position + 1 == len(commits)
                 or not commits[position + 1].startswith(revision))
        )

    def to_wire(self) -> JsonValue:
        return [
            1,
            FieldCodec.encode(self.source, GitGraphCacheSource),
            list(self.heads),
            [[commit.sha, list(commit.parents), commit.at, commit.message]
             for commit in self.commits],
            [[posting.word, list(posting.positions)] for posting in self.postings],
        ]

    @classmethod
    def from_wire(cls, data: object) -> GitHistorySnapshot:
        if (not isinstance(data, list) or len(data) != 5
                or not isinstance(data[0], int) or isinstance(data[0], bool) or data[0] != 1):
            raise ValueError("Git history cache must have supported versioned fields")
        source = FieldCodec.decode(GitGraphCacheSource, data[1])
        raw_heads, raw_commits, raw_postings = data[2:]
        if not all(isinstance(value, list) for value in (raw_heads, raw_commits, raw_postings)):
            raise ValueError("Git history cache collections must be arrays")
        if any(not isinstance(value, str) for value in raw_heads):
            raise ValueError("Git history cache heads must be commit text")
        heads = tuple(GitCommitText.from_text(value) for value in raw_heads)
        commits: list[GitCommit] = []
        for row in raw_commits:
            if not isinstance(row, list) or len(row) != 4 or not isinstance(row[1], list):
                raise ValueError("Git history cache commit rows are malformed")
            sha, parents, at, message = row
            if (not isinstance(sha, str) or any(not isinstance(parent, str) for parent in parents)
                    or isinstance(at, bool) or not isinstance(at, (int, float))
                    or not isinstance(message, str)):
                raise ValueError("Git history cache commit facts are malformed")
            commits.append(GitCommit(
                GitCommitText.from_text(sha),
                tuple(GitCommitText.from_text(parent) for parent in parents),
                float(at), message,
            ))
        postings: list[CommitWordPosting] = []
        for row in raw_postings:
            if (not isinstance(row, list) or len(row) != 2
                    or not isinstance(row[0], str) or not isinstance(row[1], list)
                    or any(isinstance(position, bool) or not isinstance(position, int)
                           for position in row[1])):
                raise ValueError("Git history cache posting rows are malformed")
            postings.append(CommitWordPosting(row[0], tuple(row[1])))
        return cls(source, heads, tuple(commits), tuple(postings))

    @classmethod
    def capturing(
        cls,
        source: GitGraphSource,
        heads: tuple[str, ...],
        graph: Mapping[str, GitCommit],
        messages: CommitMessages,
    ) -> GitHistorySnapshot:
        positions = {commit.sha: position for position, commit in enumerate(graph.values())}
        return cls(
            GitGraphCacheSource.capturing(source), heads, tuple(graph.values()),
            messages.cache_postings(positions),
        )

    def graph(self) -> CapturedCommitGraph:
        return CapturedCommitGraph(
            self.source.graph_source(), {commit.sha: commit for commit in self.commits})

    def messages(self) -> CommitMessages:
        return RestoredCommitMessages(self.commits, self.postings)


@dataclass(frozen=True, slots=True)
class _RetainedHistoryCut:
    """Identity of the snapshot last read from or written to disk, without retaining postings."""

    source: GitGraphSource
    heads: tuple[str, ...]
    commits: tuple[GitCommit, ...]


class GitHistoryCache:
    """One atomic current graph per Git common directory; never a version pile."""

    maximum_bytes: ClassVar[int] = 64 * 1024 * 1024
    relative_path: ClassVar[Path] = Path("agent-comms/history.json")

    def __init__(self, repository: Path) -> None:
        self.path = repository / self.relative_path
        self._retained: _RetainedHistoryCut | None = None

    def read(self, source: GitGraphSource) -> GitHistorySnapshot | None:
        try:
            if self.path.stat().st_size > self.maximum_bytes:
                return None
            decoded = FieldCodec.decode(
                GitHistorySnapshot, FieldCodec.parse(self.path.read_text()))
        except (OSError, TypeError, ValueError):
            return None
        if decoded.source.graph_source() != source:
            return None
        self._retained = _RetainedHistoryCut(
            decoded.source.graph_source(), decoded.heads, decoded.commits,
        )
        return decoded

    def write(self, snapshot: GitHistorySnapshot) -> None:
        cut = _RetainedHistoryCut(
            snapshot.source.graph_source(), snapshot.heads, snapshot.commits,
        )
        if cut == self._retained:
            return
        try:
            # store_files reaches process launch, which already imports Checkout.
            from agent_comms.storage.store_files import AtomicFile
            encoded = FieldCodec.compact(FieldCodec.encode(snapshot, GitHistorySnapshot))
            if len(encoded.encode("utf-8")) > self.maximum_bytes:
                return
            AtomicFile(self.path).write_text(encoded)
        except OSError:
            # This derived acceleration never owns whether the source can be answered.
            return
        self._retained = cut

    def retains(
        self,
        source: GitGraphSource,
        heads: tuple[str, ...],
        graph: Mapping[str, GitCommit],
    ) -> bool:
        return (
            self._retained is not None
            and self._retained.source == source
            and self._retained.heads == heads
            and self._retained.commits == tuple(graph.values())
        )


class CheckoutHistory:
    """A pass's selected refs and immutable commit graph, never a command-output cache.

    Acquisitions declare additional heads before the graph is first consumed. A later
    acquisition cannot silently change an answer already observed in this cut.
    """

    @classmethod
    def capture(cls, checkout: Checkout, previous: CheckoutHistory | None = None) -> Self:
        """Read a stable source cut and retain the exact files that keep it valid."""
        for _attempt in range(ChangedSources.reconcile_attempts):
            previous_revision = None if previous is None else previous.source_revision
            before = GitHistoryRevision.capture(checkout, previous_revision)
            history = cls(checkout)
            if previous is not None and previous_revision is not None:
                if before.same_refs(previous_revision):
                    history.__dict__[CheckoutHistory.refs.attrname] = previous.refs
                if before.same_heads(previous_revision):
                    history.__dict__[CheckoutHistory.heads.attrname] = previous.heads
                if before.graph == previous_revision.graph:
                    history.__dict__[CheckoutHistory.policy.attrname] = previous.policy
            history.refs, history.heads, history.policy
            after = GitHistoryRevision.capture(checkout, before)
            if before == after:
                history.source_revision = after
                return history
        raise OSError("Git history sources did not stabilize while reading refs and policy")

    def __init__(self, checkout: Checkout) -> None:
        self.checkout = checkout
        self.source_revision: GitHistoryRevision | None = None
        self.required: set[str] = set()
        self._ancestry: dict[frozenset[str], int] = {}
        self._remote_selections: dict[str, int] = {}
        self._bases: dict[int, tuple[str, ...]] = {}
        self._walks: dict[CommitSelection, CommitWalk] = {}
        self._message_seed: CommitMessages = NoCommitMessages()
        self._graph_seed: CommitGraph = CommitGraph()
        self._cache: GitHistoryCache | None = None

    def sources_current(self) -> bool:
        return self.source_revision is not None and self.source_revision.current()

    @cached_property
    def repository(self) -> Repo:
        """Origin and source identity shared by every alias of this repository."""
        return Repo.of(self.checkout.path)

    @cached_property
    def refs(self) -> Mapping[str, GitReference]:
        return MappingProxyType(GitReference.read(
            self.checkout.common_dir, "refs/heads/", "refs/remotes/"))

    @cached_property
    def policy(self) -> GitHistoryPolicy:
        """Retain replacement refs, their effective setting and original boundary bytes."""
        common = self.checkout.common_dir
        boundaries = tuple(Path(Repo.git(
            common, "rev-parse", "--path-format=absolute", "--git-path", name,
        ).removesuffix("\n")) for name in ("shallow", "info/grafts"))
        return GitHistoryPolicy(
            tuple(GitReference.read(common, "refs/replace/").values()),
            Repo.git(self.checkout.path, "config", "--type=bool", "--default", "true", "--get",
                     "core.useReplaceRefs").strip(),
            tuple((path, DocumentBytes.capture(path)) for path in boundaries),
        )

    @cached_property
    def heads(self) -> Mapping[Path, GitHead]:
        """One listing supplies every linked worktree's distinct HEAD before the graph cut."""
        listed = Repo.git(self.checkout.common_dir, "worktree", "list", "--porcelain", "-z")
        return MappingProxyType({head.checkout.path: head for head in (
            GitHead.listed(record) for record in listed.split("\0\0")
            if record and "bare" not in record.split("\0"))})

    def require_read(self) -> Mapping[str, GitReference]:
        """Read the ref cut, propagating unreadable-checkout errors to its caller."""
        return self.refs

    def following(self, fresh: CheckoutHistory) -> CheckoutHistory:
        """Only agreeing Git sources share a graph; acquired message work can survive."""
        if (fresh.refs == self.refs and fresh.heads == self.heads
                and fresh.policy == self.policy):
            self.source_revision = fresh.source_revision
            self.__dict__.pop(CheckoutHistory.graph_request.attrname, None)
            return self
        fresh._graph_seed = self.__dict__.get(CheckoutHistory.graph.attrname, self._graph_seed)
        fresh._message_seed = self._message_seed.acquired_index()
        return fresh

    def head_for(self, checkout: Checkout) -> GitHead:
        """Select by the worktree identity; shared refs cannot answer its HEAD."""
        return self.heads[checkout.resolved().path]

    def include(self, *heads: str) -> None:
        """Prepare acquired objects before observation; an already read graph stays immutable."""
        self.required.update(heads)

    @cached_property
    def graph_request(self) -> tuple[GitGraphSource, tuple[str, ...]]:
        # Capture sources before Git walks; a later cut never rereads this one's policy.
        revision = (
            self.source_revision.graph_revision(self.checkout)
            if self.source_revision is not None
            else None
        )
        source = GitGraphSource(self.checkout.common_dir, self.policy, revision)
        heads = tuple(dict.fromkeys((
            *self.required, *(reference.commit for reference in self.refs.values()),
            *(sha for head in self.heads.values() for sha in head.required()),
        )))
        return source, heads

    @cached_property
    def graph(self) -> Mapping[str, GitCommit]:
        source, heads = self.graph_request
        if type(self._graph_seed) is CommitGraph:
            self._cache = GitHistoryCache(source.repository)
            restored = self._cache.read(source)
            if restored is not None:
                self._graph_seed = restored.graph()
                self._message_seed = restored.messages()
        return self._graph_seed.following(source, heads)

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

    def walk(self, selection: CommitSelection) -> CommitWalk:
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
            self._walks[selection] = CommitWalk(tuple(commits))
        return self._walks[selection]

    @cached_property
    def messages(self) -> CommitMessages:
        captured = self._message_seed.following(tuple(self.graph.values()))
        source, heads = self.graph_request
        cache = self._cache or GitHistoryCache(source.repository)
        if not cache.retains(source, heads, self.graph):
            cache.write(GitHistorySnapshot.capturing(source, heads, self.graph, captured))
        self._message_seed = captured
        self._cache = cache
        return captured

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
        # Acquired immutable ids already name the objects in this cut. Ref names
        # still resolve through its captured refs before Git answers the pair.
        tips = tuple(revision if revision in self.required and Repo.full_commit.fullmatch(revision)
                     else self.resolve(revision) for revision in (left, right))
        try:
            bases = tuple(self.graph_request[0].git("merge-base", "--all", *tips).splitlines())
        except GitCommandError as error:
            if not error.unrelated_history():
                raise
            return ()
        if len(bases) < 2:
            return bases
        # Several best bases retain the established timestamp and contender order.
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
