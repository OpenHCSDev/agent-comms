"""Branches holding work that is not on main, and what links each one.

Tristan's rule (2026-10-16): work on a problem is on main, or on a branch with
an open PR or an open issue linked to it; never on a branch with neither. This
module derives that answer on each read from the forge and from the checkout's
worktrees. Nothing here is stored.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from .cli_protocol import WireModel
from .declared_family import DeclaredFamily
from .field_codec import projected
from .repo import Repo

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


@dataclass(frozen=True)
class ThreadBranchCheckout(BranchCheckout):
    threads: tuple[str, ...]
    worktree: str


@dataclass(frozen=True)
class UnregisteredBranchCheckout(BranchCheckout):
    """Checked out in a worktree that no registered thread works in."""

    worktree: str


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


class ForgeAnswer(WireModel):
    """GitHub GraphQL answers keep GitHub's camelCase field names."""

    model_config = ConfigDict(strict=True, alias_generator=to_camel, extra="forbid")


class PageInfo(ForgeAnswer):
    has_next_page: bool
    end_cursor: str | None

    @property
    def following(self) -> tuple[tuple[str, ...], ...]:
        """The next page's cursor arguments, or none after the last page."""
        return ((self.end_cursor,),) if self.has_next_page else ()


class Connection[Node](ForgeAnswer):
    nodes: tuple[Node, ...]
    page_info: PageInfo | None = None


class ForgePullRequest(ForgeAnswer):
    number: int
    head_ref_name: str
    is_cross_repository: bool

    @property
    def branch_links(self) -> tuple[tuple[str, PullRequestBranchLink], ...]:
        # A fork's identically spelled branch is not this origin's branch.
        return () if self.is_cross_repository else (
            (self.head_ref_name, PullRequestBranchLink(self.number)),)


class ForgeRefName(ForgeAnswer):
    name: str


class ForgeLinkedBranch(ForgeAnswer):
    ref: ForgeRefName


class ForgeIssue(ForgeAnswer):
    number: int
    linked_branches: Connection[ForgeLinkedBranch]


class PullsRepository(ForgeAnswer):
    pull_requests: Connection[ForgePullRequest]

    @property
    def connection(self) -> Connection[ForgePullRequest]:
        return self.pull_requests


class IssuesRepository(ForgeAnswer):
    issues: Connection[ForgeIssue]

    @property
    def connection(self) -> Connection[ForgeIssue]:
        return self.issues


class ForgeData[Repository](ForgeAnswer):
    repository: Repository


class ForgeQuery[Repository](ForgeAnswer):
    data: ForgeData[Repository]


@dataclass(frozen=True)
class WorkBranches:
    """One repository's branches with commits that main does not have."""

    checkout: Path
    main: ClassVar[str] = "main"
    timeout: ClassVar[float] = 60.0

    _PULLS: ClassVar[str] = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { number headRefName isCrossRepository }
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

    def _run(self, *command: str, input: str = "") -> str:
        result = subprocess.run(
            command,
            cwd=self.checkout,
            capture_output=True,
            input=input.encode(),
            timeout=self.timeout,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip() or f"{command[0]} failed"
            raise WorkBranchError(f"{' '.join(command[:3])} in {self.checkout}: {detail}")
        return result.stdout.decode()

    @property
    def repository(self) -> tuple[str, str]:
        repository = Repo.of(self.checkout).github
        return repository.owner, repository.name

    def _pages(self, query: str, answer: type[ForgeQuery], **variables: str):
        pages: list[tuple[str, ...]] = [()]
        for after in pages:
            arguments = ["gh", "api", "graphql", "-f", f"query={query}"]
            for key, value in {**variables, **dict(zip(("after",), after, strict=False))}.items():
                arguments += ["-F", f"{key}={value}"]
            connection = answer.model_validate_json(
                self._run(*arguments)
            ).data.repository.connection
            yield from connection.nodes
            pages.extend(connection.page_info.following)

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
        threads = tuple(
            sorted(
                thread.name
                for thread in snapshot.threads.values()
                if str(Path(thread.worktree).expanduser().resolve()) == resolved
            )
        )
        if not threads:
            return UnregisteredBranchCheckout(worktree)
        return ThreadBranchCheckout(threads, worktree)

    def read(self, snapshot: RegistrySnapshot) -> tuple[WorkBranch, ...]:
        """Every branch with commits not on main, most recently committed first."""
        owner, name = self.repository
        refs = {
            ref.removeprefix("refs/heads/"): oid
            for oid, ref in (
                line.split()
                for line in self._run("git", "ls-remote", "--heads", "origin").splitlines()
            )
        }
        main = refs[self.main]
        # Git already owns commit ancestry. Ask it once for all locally named
        # commits, rather than asking GitHub to compare every branch separately.
        objects = self._run(
            "git",
            "cat-file",
            "--batch-check=%(objectname) %(objecttype)",
            input="\n".join(set(refs.values())) + "\n",
        )
        missing = [line.split()[0] for line in objects.splitlines() if line.endswith(" missing")]
        if missing:
            self._run("git", "fetch", "--no-tags", "origin", *missing)
        commits = {
            oid: (int(ahead), date)
            for oid, date, ahead, _behind in (
                line.split()
                for line in self._run(
                    "git",
                    "for-each-ref",
                    f"--format=%(objectname) %(committerdate:iso-strict) %(ahead-behind:{main})",
                    "refs/heads",
                    "refs/remotes/origin",
                ).splitlines()
            )
        }
        links: dict[str, list[BranchLink]] = {}
        for pull in self._pages(self._PULLS, ForgeQuery[PullsRepository], owner=owner, name=name):
            for branch, link in pull.branch_links:
                links.setdefault(branch, []).append(link)
        for issue in self._pages(
            self._ISSUES, ForgeQuery[IssuesRepository], owner=owner, name=name
        ):
            for linked in issue.linked_branches.nodes:
                links.setdefault(linked.ref.name, []).append(IssueBranchLink(issue.number))
        worktrees = self._worktrees()
        branches = []
        for branch, oid in refs.items():
            if oid not in commits:
                # A branch published since the last local fetch has no local
                # ref yet. Compare its captured object, never an older ref.
                commits[oid] = (
                    int(self._run("git", "rev-list", "--count", f"{main}..{oid}")),
                    self._run("git", "show", "-s", "--format=%cI", oid).strip(),
                )
            ahead, date = commits[oid]
            if branch != self.main and ahead:
                branches.append(
                    WorkBranch(
                        branch,
                        ahead,
                        datetime.fromisoformat(date).astimezone(UTC).isoformat().replace(
                            "+00:00", "Z"),
                        tuple(links.get(branch, ())),
                        tuple(self._checkout(snapshot, path) for path in worktrees.get(branch, ())),
                    )
                )
        return tuple(sorted(branches, key=lambda branch: branch.committed_at, reverse=True))
