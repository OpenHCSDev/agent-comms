"""Branches holding work that is not on main, and what links each one.

Tristan's rule (2026-10-16): work on a problem is on main, or on a branch with
an open PR or an open issue linked to it; never on a branch with neither. This
module derives that answer on each read from the forge and from the checkout's
worktrees. Nothing here is stored.
"""

from __future__ import annotations

import json
import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import projected

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot


class WorkBranchError(ValueError):
    """The checkout or the forge could not answer."""


class BranchLink(DeclaredFamily, affix="BranchLink"):
    """Why a branch's work can be found again."""


@dataclass(frozen=True)
class PullRequestBranchLink(BranchLink):
    number: int


@dataclass(frozen=True)
class IssueBranchLink(BranchLink):
    number: int


class BranchCheckout(DeclaredFamily, affix="BranchCheckout"):
    """Where a branch is checked out; the owner is whoever works there."""

    @property
    @abstractmethod
    def summary(self) -> str: ...


@dataclass(frozen=True)
class ThreadBranchCheckout(BranchCheckout):
    threads: tuple[str, ...]
    worktree: str

    @property
    def summary(self) -> str:
        return ", ".join(self.threads)


@dataclass(frozen=True)
class UnregisteredBranchCheckout(BranchCheckout):
    """Checked out in a worktree that no registered thread works in."""

    worktree: str

    @property
    def summary(self) -> str:
        return f"no thread ({self.worktree})"


@dataclass(frozen=True)
class WorkBranch:
    name: str
    unmerged_commits: int
    committed_at: str
    links: tuple[BranchLink, ...]
    checkouts: tuple[BranchCheckout, ...]

    @projected(view="wire")
    def linked(self) -> bool:
        return bool(self.links)

    @property
    def owners(self) -> str:
        return ", ".join(checkout.summary for checkout in self.checkouts) or "not checked out"


@dataclass(frozen=True)
class WorkBranches:
    """One repository's branches with commits that main does not have."""

    checkout: Path
    main: ClassVar[str] = "main"
    timeout: ClassVar[float] = 60.0

    _REFS: ClassVar[str] = """
query($owner: String!, $name: String!, $main: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target { ... on Commit { committedDate } }
        compare(headRef: $main) { behindBy }
        associatedPullRequests(states: OPEN, first: 50) { nodes { number headRefName } }
      }
    }
  }
}"""
    _ISSUES: ClassVar[str] = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { number linkedBranches(first: 20) { nodes { ref { name } } } }
    }
  }
}"""

    def _run(self, *command: str) -> str:
        result = subprocess.run(command, cwd=self.checkout, capture_output=True, timeout=self.timeout)
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip() or f"{command[0]} failed"
            raise WorkBranchError(f"{' '.join(command[:3])} in {self.checkout}: {detail}")
        return result.stdout.decode()

    @property
    def repository(self) -> tuple[str, str]:
        owner, name = self._run("gh", "repo", "view", "--json", "nameWithOwner", "-q",
                                ".nameWithOwner").strip().split("/")
        return owner, name

    def _pages(self, query: str, field: str, **variables: str):
        after: str | None = None
        while True:
            arguments = ["gh", "api", "graphql", "-f", f"query={query}"]
            for key, value in {**variables, **({"after": after} if after else {})}.items():
                arguments += ["-F", f"{key}={value}"]
            connection = json.loads(self._run(*arguments))["data"]["repository"][field]
            yield from connection["nodes"]
            if not connection["pageInfo"]["hasNextPage"]:
                return
            after = connection["pageInfo"]["endCursor"]

    def _worktrees(self) -> dict[str, tuple[str, ...]]:
        """Branch name -> worktree paths that have it checked out."""
        checked: dict[str, list[str]] = {}
        path = None
        for line in self._run("git", "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ")
            elif line.startswith("branch refs/heads/") and path is not None:
                checked.setdefault(line.removeprefix("branch refs/heads/"), []).append(path)
        return {branch: tuple(paths) for branch, paths in checked.items()}

    @staticmethod
    def _checkout(snapshot: RegistrySnapshot, worktree: str) -> BranchCheckout:
        resolved = str(Path(worktree).resolve())
        threads = tuple(sorted(thread.name for thread in snapshot.threads.values()
                               if str(Path(thread.worktree).expanduser().resolve()) == resolved))
        if not threads:
            return UnregisteredBranchCheckout(worktree)
        return ThreadBranchCheckout(threads, worktree)

    def read(self, snapshot: RegistrySnapshot) -> tuple[WorkBranch, ...]:
        """Every branch with commits not on main, most recently committed first."""
        owner, name = self.repository
        issue_links: dict[str, list[BranchLink]] = {}
        for issue in self._pages(self._ISSUES, "issues", owner=owner, name=name):
            for linked in issue["linkedBranches"]["nodes"]:
                issue_links.setdefault(linked["ref"]["name"], []).append(
                    IssueBranchLink(issue["number"]))
        worktrees = self._worktrees()
        branches = []
        for ref in self._pages(self._REFS, "refs", owner=owner, name=name, main=self.main):
            # ref.compare(main).behindBy counts the branch's commits that main lacks.
            if ref["name"] == self.main or ref["compare"] is None or not ref["compare"]["behindBy"]:
                continue
            branches.append(WorkBranch(
                name=ref["name"],
                unmerged_commits=ref["compare"]["behindBy"],
                committed_at=ref["target"]["committedDate"],
                # A ref's associated PRs include those that only use it as their base.
                links=(*(PullRequestBranchLink(pull["number"])
                         for pull in ref["associatedPullRequests"]["nodes"]
                         if pull["headRefName"] == ref["name"]),
                       *issue_links.get(ref["name"], ())),
                checkouts=tuple(self._checkout(snapshot, path)
                                for path in worktrees.get(ref["name"], ())),
            ))
        return tuple(sorted(branches, key=lambda branch: branch.committed_at, reverse=True))
