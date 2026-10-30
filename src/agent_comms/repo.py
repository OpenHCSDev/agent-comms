"""Repository facts shared by branch readers and commander configuration."""

from __future__ import annotations

import heapq
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, InstanceOf, PlainSerializer, PlainValidator

from .coordination_errors import CommandFailure
from .declared_family import DeclaredFamily
from .errors import MissingSourceLine
from .field_codec import FieldCodec, GitCommitText, PresentMember, SingleFieldValue, WireValue
from .native_build_inputs import GitLaunch
from .repository_name import RepositoryName

if TYPE_CHECKING:
    from .todos import Todo
    from .work_branches import WorkBranches


class RepositorySelection(ABC):
    """A repository choice is applied by the native project it configures."""

    @abstractmethod
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]: ...

    @abstractmethod
    def founding(self, commander: str) -> tuple[Repo, ...]: ...

    @abstractmethod
    def without_commander(self) -> None: ...


class GitCommandError(RuntimeError, CommandFailure):
    """An unreadable Git input; concrete failures own the evidence for narrower answers."""

    def source_path_absent(self, sha: str, file: str) -> bool:
        return False

    def no_such_revision(self) -> bool:
        return False

    def no_check_ran_on(self, sha: str) -> bool:
        return False

    def unrelated_history(self) -> bool:
        return False


class FailedGitCommand(GitCommandError):
    """A completed Git process owns its exit status and original stderr."""

    def __init__(self, repository: str, run: subprocess.CompletedProcess[bytes]):
        self.returncode = run.returncode
        self.stderr = run.stderr.decode(errors="replace")
        detail = self.stderr.strip() or f"git failed with exit {run.returncode}"
        super().__init__(f"Git in {repository}: {detail}")

    def source_path_absent(self, sha: str, file: str) -> bool:
        """Only Git's missing-path result, never a failed fetch or corrupt repository."""
        return self.returncode == 128 and self.stderr in (
            f"fatal: path '{file}' does not exist in '{sha}'\n",
            f"fatal: path '{file}' exists on disk, but not in '{sha}'\n",
        )

    def no_such_revision(self) -> bool:
        """``rev-parse --verify --quiet``'s unresolved revision, not a broken repository."""
        return self.returncode == 1 and not self.stderr

    def no_check_ran_on(self, sha: str) -> bool:
        """Git's absent-note reply, not a corrupt repository or an invalid revision."""
        return self.returncode == 1 and self.stderr in (
            "", f"error: no note found for object {sha}.\n",
        )

    def unrelated_history(self) -> bool:
        return self.returncode == 1


class UnavailableCommitError(GitCommandError):
    """Required Git objects are unavailable, not a negative ancestry or absent-path answer."""

    def __init__(self, checkout: Path | str, objects: tuple[str, ...], detail: str = ""):
        super().__init__(f"Git in {checkout}: required Git objects unavailable: "
                         f"{', '.join(objects)}" + (f"; {detail}" if detail else ""))


class Repo(GitLaunch, BaseModel, WireValue):
    """A repository the fleet works on: its check checkout, its GitHub name and its source root."""

    model_config = ConfigDict(frozen=True)
    remote: ClassVar[str] = "origin"
    local_remote: ClassVar[str] = "."

    checkout: Path
    github: InstanceOf[
        Annotated[
            RepositoryName,
            PlainValidator(RepositoryName.from_wire, json_schema_input_type=str),
            PlainSerializer(lambda name: name.to_wire()),
        ]
    ]
    source: str = "src"

    @classmethod
    def command(cls, checkout: Path | str, *args: str) -> tuple[str, ...]:
        """Only explicit acquisition may contact a promisor remote for missing objects."""
        return super().command(checkout, "--no-lazy-fetch", *args)

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls.model_validate_json(FieldCodec.compact(data))

    @classmethod
    def selection_refusal(cls) -> str:
        return "Select --repo when more than one repository is registered."

    @classmethod
    def selected(cls, repositories: tuple[Repo, ...], name: str) -> Repo:
        candidates = cls.candidates(repositories, name)
        if len(candidates) != 1:
            raise ValueError(cls.selection_refusal())
        repository, = candidates
        return repository

    @classmethod
    def candidates(cls, repositories: tuple[Repo, ...], name: str) -> tuple[Repo, ...]:
        """No name leaves every registered repository a candidate; a name selects exactly one."""
        if not name:
            return repositories
        for repository in repositories:
            if repository.named(name):
                return (repository,)
        configured = ", ".join(
            f"{repository.github.to_wire()} ({repository.checkout})"
            for repository in repositories
        )
        raise ValueError(f"Unknown repository {name!r}. Registered repositories: {configured}. "
                         "Set repositories with agent-comms install --repos.")

    @classmethod
    def declaring(cls, repositories: tuple[Repo, ...], module: str) -> Repo:
        for repository in repositories:
            if (repository.checkout / repository.module_file(module)).is_file():
                return repository
        raise ValueError(f"No registered repository holds {module}")

    def module_file(self, module: str) -> Path:
        """Where the dotted ``module`` sits under this checkout's source root, relative to it."""
        return Path(self.source, *module.split(".")).with_suffix(".py")

    def named(self, name: str) -> bool:
        """A repository name or path identifies this declared checkout, not another worktree."""
        return self.github.named(name) or self.checkout == Path(name).resolve()

    @property
    def checked_out(self) -> bool:
        """The checkout is a work tree of its own; a removed submodule leaves an empty directory
        that git would resolve to the enclosing repository instead."""
        return (self.checkout / ".git").exists()

    def holds(self, todo: Todo) -> bool:
        return self.github == todo.repo

    def default_branch(self) -> str:
        """The origin's declared default, never a guessed branch name."""
        try:
            reference = self.git(
                self.checkout, "symbolic-ref", "--short", f"refs/remotes/{self.remote}/HEAD",
            ).strip()
        except GitCommandError as error:
            raise ValueError(
                f"Origin's default branch is unset in {self.checkout}; "
                "run git remote set-head origin --auto in that checkout."
            ) from error
        return reference.removeprefix(f"{self.remote}/")

    @classmethod
    def missing_commits(cls, checkout: Path | str, *objects: str) -> tuple[str, ...]:
        """Resolve Git expressions, including abbreviated SHAs, as commit objects in one batch."""
        requested = tuple(dict.fromkeys(objects))
        if not requested:
            return ()
        answers = cls.git(checkout, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                          input="".join(f"{name}^{{commit}}\n" for name in requested))
        return tuple(name for name, answer in zip(requested, answers.splitlines(), strict=True)
                     if not answer.endswith(" commit"))

    @classmethod
    def require_local_commits(cls, checkout: Path | str, *objects: str) -> None:
        """Observation never acquires objects or mistakes an unavailable one for False."""
        missing = cls.missing_commits(checkout, *objects)
        if missing:
            raise UnavailableCommitError(checkout, missing)

    @classmethod
    def require_commits(cls, checkout: Path | str, remote: str, *objects: str) -> None:
        """The explicit acquisition boundary: fetch missing objects, then prove availability."""
        missing = cls.missing_commits(checkout, *objects)
        if missing:
            try:
                cls.git(checkout, "fetch", "--quiet", "--no-tags", "--", remote, *missing)
            except GitCommandError as error:
                raise UnavailableCommitError(checkout, missing, f"remote {remote}: {error}") from error
            cls.require_local_commits(checkout, *objects)

    def contains(self, sha: str, revision: str = "HEAD") -> bool:
        """Strict local ancestry; only valid local non-ancestors mean False."""
        self.require_local_commits(self.checkout, sha, revision)
        return self.descends(self.checkout, sha, revision)

    def has_commit(self, sha: str) -> bool:
        return self.answers(self.checkout, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")

    @classmethod
    def tree_objects(cls, checkout: Path | str, revision: str, paths: tuple[str, ...],
                     *options: str) -> dict[str, str]:
        """Decode selected tree entries without requiring their contents to be local."""
        entries = cls.git(checkout, "ls-tree", "-z", *options, revision, "--", *paths)
        return {path: metadata.rsplit(" ", 1)[1]
                for metadata, path in (entry.split("\t", 1)
                                       for entry in entries.split("\0") if entry)}

    @classmethod
    def blobs(cls, checkout: Path | str, *objects: str, timeout: float = 60) -> tuple[bytes, ...]:
        """Decode required contents once; an unavailable blob is never an absent path."""
        if not objects:
            return ()
        batch = cls.git_bytes(checkout, "cat-file", "--batch", "-Z", timeout=timeout,
                              input="".join(f"{name}\0" for name in objects))
        contents: list[bytes] = []
        offset = 0
        for name in objects:
            end = batch.index(b"\0", offset)
            header = batch[offset:end]
            offset = end + 1
            if header.endswith(b" missing"):
                raise UnavailableCommitError(checkout, (name,), "required blob unavailable")
            size = int(header.rsplit(b" ", 1)[1])
            contents.append(batch[offset:offset + size])
            offset += size + 1
        return tuple(contents)

    @classmethod
    def git_bytes(cls, checkout: Path | str, *args: str, timeout: float = 60,
                  input: str = "") -> bytes:
        """Git's exact output, for answers sized in bytes (``cat-file --batch``); every
        failure a GitCommandError."""
        return cls._output(checkout, cls._run(checkout, *args, timeout=timeout, input=input.encode()))

    @staticmethod
    def _output(checkout: Path | str, run: subprocess.CompletedProcess[bytes]) -> bytes:
        """The shared failure boundary for captured and streamed Git answers."""
        if run.returncode:
            raise FailedGitCommand(str(checkout), run)
        return run.stdout

    @classmethod
    def git_pipeline(cls, checkout: Path | str, producer: tuple[str, ...], consumer: tuple[str, ...],
                     *, timeout: float = 60, input: str = '') -> str:
        return cls._output(checkout, cls._pipe(checkout, producer, consumer,
                           timeout=timeout, input=input.encode())).decode()

    @classmethod
    def git(cls, checkout: Path | str, *args: str, timeout: float = 60, input: str = "") -> str:
        """Git's output as text; every failure a GitCommandError."""
        return cls.git_bytes(checkout, *args, timeout=timeout, input=input).decode()

    @classmethod
    def answers(cls, checkout: Path | str, *args: str) -> bool:
        """A yes-or-no Git question: exit 0 is yes, exit 1 is no, and any other exit (no
        repository, an unreadable object store) is a GitCommandError, never a no."""
        run = cls._run(checkout, *args)
        if run.returncode not in (0, 1):
            raise FailedGitCommand(str(checkout), run)
        return run.returncode == 0

    @classmethod
    def descends(cls, checkout: Path | str, ancestor: str, revision: str) -> bool:
        """Whether ``revision`` holds ``ancestor`` in this checkout's history."""
        return cls.answers(checkout, "merge-base", "--is-ancestor", ancestor, revision)

    @classmethod
    def store_serves(cls, store: Path, *args: str) -> bool:
        """A question to one submodule store, pinned as the git directory so Git never
        discovers the repository around it: any failure, a store holding no repository
        included, means the store cannot serve."""
        return cls._run(store, "--git-dir", ".", *args).returncode == 0

    @classmethod
    def grep(cls, checkout: Path | str, *args: str) -> tuple[str, ...]:
        """``git grep`` rows: exit 1 means nothing matched."""
        run = cls._run(checkout, "grep", *args)
        if run.returncode == 1:
            return ()
        if run.returncode:
            raise FailedGitCommand(str(checkout), run)
        return tuple(run.stdout.decode().splitlines())

    @classmethod
    def merge_conflicts(cls, checkout: Path | str, left: str, right: str) -> tuple[str, ...]:
        """The paths ``git merge-tree`` cannot merge between two commits: exit 0 merges
        cleanly, exit 1 lists the conflicting paths after the written tree."""
        run = cls._run(checkout, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                       left, right)
        if run.returncode == 1:
            return tuple(run.stdout.decode().splitlines()[1:])
        if run.returncode:
            raise FailedGitCommand(str(checkout), run)
        return ()

    @classmethod
    def of(cls, checkout: Path) -> Repo:
        """Its GitHub name comes from the checkout's own origin remote."""
        url = cls.git(checkout, "remote", "get-url", cls.remote).strip()
        github = url.removesuffix(".git").replace(":", "/").split("/")[-2:]
        return cls(checkout=checkout, github=RepositoryName.from_wire("/".join(github)))


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

    def lines(self, commit: str, file: str) -> tuple[str, ...]:
        """Read the parent's pinned object, independent of the module's working tree."""
        module = self.path.relative_to(self.checkout).as_posix()
        pinned = Repo.git(self.checkout, 'rev-parse', f'{commit}:{module}').strip()
        relative = (self.checkout / file).relative_to(self.path).as_posix()
        store = self.store
        Repo.git(self.checkout, '--git-dir', str(store), 'rev-parse', '--verify',
                 f'{pinned}^{{commit}}')
        try:
            text = Repo.git(self.checkout, '--git-dir', str(store),
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

    def usable(self) -> bool:
        """A repository that can serve every later update: it holds a commit, and it has the
        origin `submodule update` fetches a newly pinned commit from. A store borrowing its
        objects through alternates with no remote and no refs answers the first and fails
        the second the moment the pin moves ("transport 'file' not allowed")."""
        return all(
            Repo.store_serves(self.store, *question)
            for question in (
                ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
                ("remote", "get-url", Repo.remote),
            )
        )

    def broken(self) -> bool:
        """Anything but a usable store or the empty directory `git worktree add` leaves: a
        store without a repository or without its origin, a `.git` pointing at one, a
        hand-copied directory or a link standing where the clone belongs."""
        occupied = self.path.is_symlink() or (self.path.is_dir() and any(self.path.iterdir()))
        return (self.store.exists() or occupied) and not self.usable()

    def clear(self) -> None:
        """Pinned package data, never a unit's edit: dropped whole so the next update
        clones it again from its declared source."""
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
        return Path(Repo.git(self.path, "rev-parse", "--path-format=absolute",
                             "--git-common-dir").strip()).resolve()

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
        transfer = CheckoutTransfer(source, destination,
                                    Repo.git(source, 'rev-parse', '--abbrev-ref', 'HEAD').strip(),
                                    Repo.git(source, 'rev-parse', 'HEAD').strip())
        with ExitStack() as rollback:
            source.rename(destination)
            rollback.callback(transfer.restore, common, modules)
            Repo.git(common, 'worktree', 'repair', str(destination))
            for module in modules:
                module.apply(destination)
            yield transfer
            rollback.pop_all()

    def prepare(self) -> None:
        """Populate the pinned submodules, rebuilding any an interrupted clone left broken.
        A tree that pins none has nothing to prepare."""
        modules = self.submodules()
        if not modules:
            return
        for module in modules:
            if module.broken():
                module.clear()
        Repo.git(self.path, "submodule", "update", "--init", "--recursive", "--checkout",
                 timeout=300)

    def held_work(self) -> str:
        """Changes outside the pinned submodules, which retiring the tree would lose."""
        return Repo.git(self.path, "status", "--porcelain", "--ignore-submodules=all")

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

    def require_prepared(self, reader: WorkBranches) -> None:
        """An absent or local upstream requires no published remote head."""

    def tracking_name(self) -> str:
        return ""

    def unpushed(self, reader: WorkBranches, head: str) -> tuple[UnpushedHead, ...]:
        # A local, absent or deleted upstream cannot make its stale tracking ref publication.
        tracking = self.tracking_name()
        count = reader.history.unpublished(head, tracking)
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

    def require_prepared(self, reader: WorkBranches) -> None:
        for head in reader.selected_heads(self.remote, self.reference):
            reader.history.commit(head)

    def unpushed(self, reader: WorkBranches, head: str) -> tuple[UnpushedHead, ...]:
        published = reader.selected_heads(self.remote, self.reference)
        if published:
            (remote_head,) = published
            count = reader.history.count(head, remote_head)
            return (UnpushedHead(head, count, self.tracking),) if count else ()
        return super().unpushed(reader, head)


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
        fields = tuple(field.partition(" ") for field in record.split("\0"))
        (path,) = tuple(value for name, _, value in fields if name == "worktree")
        (sha,) = tuple(value for name, _, value in fields if name == cls.reference)
        symbolic = next((value for name, _, value in fields if name == "branch"), "")
        checkout = Checkout(Path(path).resolve())
        commit = GitCommitText.decode(sha)
        if not commit.strip("0"):
            return UnbornGitHead(checkout, symbolic)
        if symbolic:
            return AttachedGitHead(checkout, sha=commit, symbolic=symbolic)
        if ("detached", "", "") not in fields:
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

    def history_for(self, checkout: Checkout) -> CheckoutHistory:
        checkout = checkout.resolved()
        if checkout.path not in self._worktree_histories:
            common = checkout.common_dir
            if common not in self._histories:
                self._histories[common] = checkout.read()
            history = self._histories[common]
            self._worktree_histories.update((path, history) for path in history.heads)
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

    def commits(self, refs: tuple[str, ...], *excluded: str,
                pattern: str = "", first_parent: bool = False) -> Iterator[GitCommit]:
        """Filter a captured newest-first walk without traversing the graph again."""
        matching = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        return (commit for commit in self.walk(CommitSelection(refs, excluded, first_parent))
                if matching.search(commit.message))

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


@dataclass(frozen=True)
class CheckoutTransfer:
    """The retained Git identity and the paths of one move."""

    source: Path
    destination: Path
    branch: str
    head: str

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
