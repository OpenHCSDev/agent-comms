"""Repository facts shared by branch readers and commander configuration."""

from __future__ import annotations

import shutil
import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, InstanceOf, PlainSerializer, PlainValidator

from .declared_family import DeclaredFamily
from .field_codec import PathText
from .repository_name import RepositoryName

if TYPE_CHECKING:
    from .todos import Todo


class Repo(BaseModel):
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
        return cls(checkout=checkout.resolve(), github=RepositoryName.from_wire("/".join(github)))


class CheckoutHoldsWork(Exception):
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

    @property
    def store(self) -> Path:
        return Path(Repo.git(self.checkout, "rev-parse", "--path-format=absolute",
                             "--git-path", f"modules/{self.name}").strip())

    def usable(self) -> bool:
        return subprocess.run(
            ("git", "--git-dir", str(self.store), "rev-parse", "--verify", "--quiet",
             "HEAD^{commit}"),
            capture_output=True, timeout=30,
        ).returncode == 0

    def broken(self) -> bool:
        """A store without a repository, or a working directory pointing at one."""
        return (self.store.exists() or (self.path / ".git").exists()) and not self.usable()

    def clear(self) -> None:
        """Pinned package data, never a unit's edit: dropped whole so the next update
        clones it again from its declared source."""
        for directory in (self.store, self.path):
            if directory.exists():
                shutil.rmtree(directory)
        self.path.mkdir(parents=True)


class CheckoutRead(DeclaredFamily, affix="Checkout"):
    """Whether a checkout answers git reads once its submodules are prepared."""

    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class ReadableCheckout(CheckoutRead):
    path: Annotated[Path, PathText]

    def lines(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class UnreadableCheckout(CheckoutRead):
    path: Annotated[Path, PathText]
    error: str

    def lines(self) -> tuple[str, ...]:
        return (f"Checkout unreadable: {self.path}: {self.error}",)


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

    @classmethod
    def of_repository(cls, repository: Path) -> tuple[Self, ...]:
        """Every existing worktree of the repository, its main checkout included."""
        listed = Repo.git(repository, "worktree", "list", "--porcelain")
        paths = (Path(line.removeprefix("worktree ")) for line in listed.splitlines()
                 if line.startswith("worktree "))
        return tuple(cls(path) for path in paths if path.is_dir())

    def submodules(self) -> tuple[PinnedSubmodule, ...]:
        if not (self.path / ".gitmodules").is_file():
            return ()
        listed = Repo.git(self.path, "config", "-f", ".gitmodules", "--get-regexp",
                          r"^submodule\..*\.path$")
        return tuple(
            PinnedSubmodule(self.path, key.removeprefix("submodule.").removesuffix(".path"),
                            self.path / value)
            for key, value in (line.split(" ", 1) for line in listed.splitlines())
        )

    def prepare(self) -> None:
        """Populate the pinned submodules, rebuilding any an interrupted clone left broken."""
        for module in self.submodules():
            if module.broken():
                module.clear()
        subprocess.run(
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
            check=True,
            timeout=300,
        )

    def read(self) -> CheckoutRead:
        """Prepared, then read the way Core reads a unit's edits."""
        try:
            self.prepare()
            Repo.git(self.path, "diff", "--name-only", "HEAD")
        except subprocess.CalledProcessError as error:
            return UnreadableCheckout(self.path, error.stderr.strip())
        except subprocess.TimeoutExpired as error:
            return UnreadableCheckout(self.path, str(error))
        return ReadableCheckout(self.path)

    def held_work(self) -> str:
        """Changes outside the pinned submodules, which retiring the tree would lose."""
        return Repo.git(self.path, "status", "--porcelain", "--ignore-submodules=all")

    def retire(self) -> None:
        """Remove this worktree. Git refuses any tree holding submodules without --force,
        so this owner checks for held work itself and then forces."""
        held = self.held_work()
        if held:
            raise CheckoutHoldsWork(self.path, held)
        common = Path(Repo.git(self.path, "rev-parse", "--path-format=absolute",
                               "--git-common-dir").strip())
        Repo.git(common.parent, "worktree", "remove", "--force", str(self.path))


class ScratchCheckout(Checkout):
    """A tree made for one run and discarded with whatever the run left in it."""

    def held_work(self) -> str:
        return ""
