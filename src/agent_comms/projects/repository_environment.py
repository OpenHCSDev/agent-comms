"""One test prefix for a Git repository, shared by all its worktrees."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.projects.repo import Repo


@dataclass(frozen=True)
class RepositoryEnvironment:
    sync_verb: ClassVar[str] = "sync-test-environment"

    common: Path
    prefix: Path

    @classmethod
    def of(cls, checkout: Path) -> RepositoryEnvironment:
        common = Repo.common_dir_of(checkout)
        # Git lists the main worktree first, including a separately located Git dir.
        # A bare repository has no main worktree; keep its environment under its store.
        primary = Repo.git(common, "worktree", "list", "--porcelain", "-z").split("\0\0", 1)[0]
        path, *attributes = primary.split("\0")
        home = Path(path.removeprefix("worktree ")).resolve()
        prefix = (common / "test-environment" if "bare" in attributes else
                  Path(Repo.git(home, "rev-parse", "--show-toplevel").strip()).resolve() / ".venv")
        return cls(common, prefix)

    @property
    def declaration_record(self) -> Path:
        return self.common / "test-environment.json"

    @property
    def history(self) -> Path:
        return self.common / "test-environment-history"
