"""Repository facts shared by branch readers and commander configuration."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Repo(BaseModel):
    """A repository the fleet works on: its check checkout, its GitHub name and its source root."""

    model_config = ConfigDict(frozen=True)

    checkout: Path
    github: str
    source: str = "src"

    def contains(self, sha: str) -> bool:
        """Only a successful ancestry proof in this installed checkout permits closure."""
        result = subprocess.run(
            ("git", "-C", str(self.checkout), "merge-base", "--is-ancestor", sha, "HEAD"),
            capture_output=True,
            timeout=60,
        )
        if result.returncode not in (0, 1):
            result.check_returncode()
        return result.returncode == 0

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
        return cls(checkout=checkout.resolve(), github="/".join(github))
