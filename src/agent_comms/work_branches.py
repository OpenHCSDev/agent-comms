"""Branches holding work that is not on main, and what links each one.

Tristan's rule (2026-10-16): work on a problem is on main, or on a branch with
an open PR or an open issue linked to it; never on a branch with neither. This
module derives that answer on each read from the forge and from the checkout's
worktrees. Nothing here is stored.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from .declared_family import DeclaredFamily
from .cli_protocol import WireModel
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


class ForgeCommit(ForgeAnswer):
    committed_date: str


class ForgeComparison(ForgeAnswer):
    behind_by: int


class ForgePullRequest(ForgeAnswer):
    number: int
    head_ref_name: str


class ForgeRef(ForgeAnswer):
    name: str
    target: ForgeCommit
    compare: ForgeComparison | None
    associated_pull_requests: Connection[ForgePullRequest]

    @property
    def unmerged_commits(self) -> int:
        # ref.compare(main).behindBy counts the branch's commits that main lacks.
        return 0 if self.compare is None else self.compare.behind_by

    @property
    def own_pull_requests(self) -> tuple[PullRequestBranchLink, ...]:
        # A ref's associated PRs include those that only use it as their base.
        return tuple(PullRequestBranchLink(pull.number)
                     for pull in self.associated_pull_requests.nodes
                     if pull.head_ref_name == self.name)


class ForgeRefName(ForgeAnswer):
    name: str


class ForgeLinkedBranch(ForgeAnswer):
    ref: ForgeRefName


class ForgeIssue(ForgeAnswer):
    number: int
    linked_branches: Connection[ForgeLinkedBranch]


class RefsRepository(ForgeAnswer):
    refs: Connection[ForgeRef]

    @property
    def connection(self) -> Connection[ForgeRef]:
        return self.refs


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

    def _pages(self, query: str, answer: type[ForgeQuery], **variables: str):
        pages: list[tuple[str, ...]] = [()]
        for after in pages:
            arguments = ["gh", "api", "graphql", "-f", f"query={query}"]
            for key, value in {**variables, **dict(zip(("after",), after))}.items():
                arguments += ["-F", f"{key}={value}"]
            connection = answer.model_validate_json(self._run(*arguments)).data.repository.connection
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
        threads = tuple(sorted(thread.name for thread in snapshot.threads.values()
                               if str(Path(thread.worktree).expanduser().resolve()) == resolved))
        if not threads:
            return UnregisteredBranchCheckout(worktree)
        return ThreadBranchCheckout(threads, worktree)

    def read(self, snapshot: RegistrySnapshot) -> tuple[WorkBranch, ...]:
        """Every branch with commits not on main, most recently committed first."""
        owner, name = self.repository
        issue_links: dict[str, list[BranchLink]] = {}
        for issue in self._pages(self._ISSUES, ForgeQuery[IssuesRepository], owner=owner, name=name):
            for linked in issue.linked_branches.nodes:
                issue_links.setdefault(linked.ref.name, []).append(IssueBranchLink(issue.number))
        worktrees = self._worktrees()
        branches = tuple(
            WorkBranch(
                name=ref.name,
                unmerged_commits=ref.unmerged_commits,
                committed_at=ref.target.committed_date,
                links=(*ref.own_pull_requests, *issue_links.get(ref.name, ())),
                checkouts=tuple(self._checkout(snapshot, path) for path in worktrees.get(ref.name, ())),
            )
            for ref in self._pages(self._REFS, ForgeQuery[RefsRepository],
                                   owner=owner, name=name, main=self.main)
            if ref.name != self.main and ref.unmerged_commits
        )
        return tuple(sorted(branches, key=lambda branch: branch.committed_at, reverse=True))
