"""Repository facts shared by branch readers and commander configuration."""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, InstanceOf, PlainSerializer, PlainValidator

from .field_codec import FieldCodec, WireValue
from .repository_name import RepositoryName

if TYPE_CHECKING:
    from .todos import Todo


class RepositorySelection(ABC):
    """A repository choice is applied by the native project it configures."""

    @abstractmethod
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]: ...

    @abstractmethod
    def founding(self, commander: str) -> tuple[Repo, ...]: ...

    @abstractmethod
    def without_commander(self) -> None: ...


class Repo(BaseModel, WireValue):
    """A repository the fleet works on: its check checkout, its GitHub name and its source root."""

    model_config = ConfigDict(frozen=True)

    checkout: Path
    github: InstanceOf[
        Annotated[
            RepositoryName,
            PlainValidator(RepositoryName.from_wire, json_schema_input_type=str),
            PlainSerializer(RepositoryName.to_wire),
        ]
    ]
    source: str = "src"

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls.model_validate_json(FieldCodec.compact(data))

    @classmethod
    def selected(cls, repositories: tuple[Repo, ...], name: str) -> Repo:
        for repository in repositories:
            if repository.named(name):
                return repository
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

    def holds(self, todo: Todo) -> bool:
        return self.github == todo.repo

    def contains(self, sha: str, revision: str = "HEAD") -> bool:
        """Prove ancestry in the requested fetched revision without moving the checkout.

        A commit merged after the last fetch is fetched once first; a commit origin does not
        have either is in no revision of this checkout."""
        if not self.has_commit(sha):
            self.git(self.checkout, "fetch", "-q", "origin")
            if not self.has_commit(sha):
                return False
        result = subprocess.run(
            ("git", "-C", str(self.checkout), "merge-base", "--is-ancestor", sha, revision),
            capture_output=True,
            timeout=60,
        )
        if result.returncode not in (0, 1):
            result.check_returncode()
        return result.returncode == 0

    def has_commit(self, sha: str) -> bool:
        return (
            subprocess.run(
                ("git", "-C", str(self.checkout), "cat-file", "-e", f"{sha}^{{commit}}"),
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )

    @staticmethod
    def git(checkout: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout

    @classmethod
    def of(cls, checkout: Path) -> Repo:
        """Its GitHub name comes from the checkout's own origin remote."""
        url = cls.git(checkout, "remote", "get-url", "origin").strip()
        github = url.removesuffix(".git").replace(":", "/").split("/")[-2:]
        return cls(checkout=checkout, github=RepositoryName.from_wire("/".join(github)))


class CheckoutHoldsWorkError(Exception):
    """A checkout cannot be retired: it holds changes that removing it would lose."""

    def __init__(self, path: Path, changes: str):
        super().__init__(f"{path} holds uncommitted work:\n{changes}")


class CheckoutPrepareError(Exception):
    """Git could not populate a checkout's pinned submodules; its own words say why."""

    def __init__(self, path: Path, stderr: str):
        super().__init__(f"{path}: git submodule update failed:\n{stderr}")


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
            subprocess.run(
                ("git", "--git-dir", str(self.store), *question),
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
            for question in (
                ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
                ("remote", "get-url", "origin"),
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


@dataclass(frozen=True)
class Checkout:
    """A working tree of one of our repositories, ready to run.

    Its pinned submodules (the refactor-audit catalog) belong to the tree, and `git worktree
    add` leaves them empty even with submodule.recurse set, so every tree is added, prepared
    and retired here and nowhere else."""

    path: Path

    @classmethod
    def add(cls, repository: Path | str, path: Path | str, revision: str, *options: str) -> Self:
        subprocess.run(
            ("git", "-C", str(repository), "worktree", "add", *options, str(path), revision),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        checkout = cls(Path(path))
        checkout.prepare()
        return checkout

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

    def prepare(self) -> None:
        """Populate the pinned submodules, rebuilding any an interrupted clone left broken.
        A tree that pins none has nothing to prepare."""
        modules = self.submodules()
        if not modules:
            return
        for module in modules:
            if module.broken():
                module.clear()
        update = subprocess.run(
            (
                "git",
                "-C",
                str(self.path),
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--checkout",
            ),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if update.returncode:
            raise CheckoutPrepareError(self.path, update.stderr)

    def held_work(self) -> str:
        """Changes outside the pinned submodules, which retiring the tree would lose."""
        return Repo.git(self.path, "status", "--porcelain", "--ignore-submodules=all")

    def retire(self) -> None:
        """Remove this worktree. Git refuses any tree holding submodules without --force,
        so this owner checks for held work itself and then forces."""
        held = self.held_work()
        if held:
            raise CheckoutHoldsWorkError(self.path, held)
        common = Path(
            Repo.git(self.path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        )
        Repo.git(common.parent, "worktree", "remove", "--force", str(self.path))


class ScratchCheckout(Checkout):
    """A tree made for one run and discarded with whatever the run left in it."""

    def held_work(self) -> str:
        return ""
