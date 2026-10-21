"""Repository facts shared by branch readers and commander configuration."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, InstanceOf, PlainSerializer, PlainValidator

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


@dataclass(frozen=True)
class Checkout:
    """A working tree of one of our repositories, ready to run.

    Its pinned submodules (the refactor-audit catalog) belong to the tree, and `git worktree
    add` leaves them empty even with submodule.recurse set, so every tree is added and
    prepared here and nowhere else."""

    path: Path

    @classmethod
    def add(cls, repository: Path | str, path: Path | str, revision: str, *options: str) -> Checkout:
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

    def prepare(self) -> None:
        """Populate the pinned submodules; an already populated tree costs one status read."""
        subprocess.run(
            ("git", "-C", str(self.path), "submodule", "update", "--init", "--recursive", "--checkout"),
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
