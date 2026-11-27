from __future__ import annotations

import shutil
from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Self

from agent_comms.errors import MissingSourceLine
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, PresentMember, SingleFieldValue
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_environment import RepositoryEnvironment


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
