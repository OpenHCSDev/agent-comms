"""Repository facts shared by branch readers and commander configuration."""

from __future__ import annotations

import os
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


class GitCommandError(RuntimeError):
    """A Git operation failure, not malformed order data to aggregate and continue past."""

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


class Repo(BaseModel, WireValue):
    """A repository the fleet works on: its check checkout, its GitHub name and its source root."""

    model_config = ConfigDict(frozen=True)

    checkout: Path
    github: InstanceOf[
        Annotated[
            RepositoryName,
            PlainValidator(RepositoryName.from_wire, json_schema_input_type=str),
            PlainSerializer(lambda name: name.to_wire()),
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
        return self.descends(self.checkout, sha, revision)

    def has_commit(self, sha: str) -> bool:
        return self.answers(self.checkout, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")

    @staticmethod
    def _run(
        checkout: Path | str, *args: str, timeout: float = 60, input: bytes = b""
    ) -> subprocess.CompletedProcess[bytes]:
        """The one Git subprocess: C-locale output, bytes in and out."""
        return subprocess.run(("git", "-C", str(checkout), *args), capture_output=True,
                              input=input, timeout=timeout, env=os.environ | {"LC_ALL": "C"})

    @classmethod
    def git_bytes(cls, checkout: Path | str, *args: str, timeout: float = 60,
                  input: str = "") -> bytes:
        """Git's exact output, for answers sized in bytes (``cat-file --batch``); every
        failure a GitCommandError."""
        run = cls._run(checkout, *args, timeout=timeout, input=input.encode())
        if run.returncode:
            raise GitCommandError(str(checkout), run)
        return run.stdout

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
            raise GitCommandError(str(checkout), run)
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
            raise GitCommandError(str(checkout), run)
        return tuple(run.stdout.decode().splitlines())

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
            Repo.store_serves(self.store, *question)
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
        Repo.git(repository, "worktree", "add", *options, str(path), revision)
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
        common = Path(
            Repo.git(self.path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        )
        Repo.git(common.parent, "worktree", "remove", "--force", str(self.path))


class ScratchCheckout(Checkout):
    """A tree made for one run and discarded with whatever the run left in it."""

    def held_work(self) -> str:
        return ""
