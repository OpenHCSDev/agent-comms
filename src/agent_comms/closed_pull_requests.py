"""Prepare closed-work answers outside the heartbeat, then publish one committed snapshot."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .head_review import PullRequestHead
from .message_bus import JoinedWatch
from .repo import Repo
from .repository_name import RepositoryName
from .todos import (
    PullRequests, StoredClosedHead, StoredClosedRepository, StoredCommitPatch,
    StoredHeadDiff, StoredMainPatch, TodoStore,
)
from .work_branches import CommitBase, UnservableCommitBase, WorkBranchError, WorkBranches

if TYPE_CHECKING:
    from .comms import Comms


@dataclass(frozen=True)
class ClosedRepositoryObservation:
    progress: StoredClosedRepository
    heads: tuple[StoredClosedHead, ...]
    fingerprints: tuple[StoredCommitPatch, ...]
    main_patches: frozenset[str]
    diffs: tuple[StoredHeadDiff, ...]

    def commit(self, store: TodoStore) -> None:
        with store.snapshot() as db:
            unchanged = (self.progress in StoredClosedRepository.select(db, repo=self.progress.repo)
                         and frozenset(self.heads) == frozenset(
                             StoredClosedHead.select(db, repo=self.progress.repo)))
        if unchanged:
            return
        # No external operation or Git subprocess is allowed inside this write.
        with store.writing() as db:
            for fingerprint in self.fingerprints:
                fingerprint.insert_if_absent(db)
            for patch in self.main_patches:
                StoredMainPatch(self.progress.repo, patch).insert_if_absent(db)
            for diff in self.diffs:
                diff.insert_if_absent(db)
            StoredClosedHead.delete(db, repo=self.progress.repo)
            for head in self.heads:
                head.insert(db)
            self.progress.upsert(db)


@dataclass(frozen=True)
class ClosedHeadScan:
    """One repository pass: main's new patches and the stored rows its heads share."""

    store: TodoStore
    reader: WorkBranches
    repo: RepositoryName
    published: str
    new_patches: frozenset[str]
    fingerprints: dict[str, StoredCommitPatch]
    unservable: tuple[str, ...]
    diffs: dict[tuple[str, str], StoredHeadDiff] = field(default_factory=dict)

    def head(self, request: PullRequestHead, closed_at: float) -> StoredClosedHead:
        base: CommitBase = (UnservableCommitBase() if request.head in self.unservable
                            else self.reader.commit_base(self.published, request.head))
        return StoredClosedHead(request, base, closed_at, base.missing(self, request.head))

    def missing_after(self, base: str, head: str) -> tuple[str, ...]:
        """Commits whose patches main lacks; none when the whole diff landed as a squash."""
        if (base, head) not in self.diffs:
            self.diffs[base, head] = StoredHeadDiff.prepare(self.store, self.reader, base, head)
        diff = self.diffs[base, head]
        candidates = StoredCommitPatch.prepare(self.store, self.reader, diff.commits,
                                                self.fingerprints)
        with self.store.snapshot() as db:
            if StoredMainPatch.contains_all(db, self.repo, diff.patches - self.new_patches):
                return ()
            return tuple(commit.commit for commit in candidates
                         if not StoredMainPatch.contains_all(
                             db, self.repo, commit.patches - self.new_patches))


class ClosedPullRequestWatch(JoinedWatch):
    """Each completed repository publishes before the next external observation starts."""

    def watch_pass(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        with opened_wire() as comms:
            for repo in comms.registry.snapshot().commander_project.unique_repositories():
                self.observe_repository(comms.goals.todos, repo).commit(comms.goals.todos)

    @staticmethod
    def observe_repository(store: TodoStore, repo: Repo) -> ClosedRepositoryObservation:
        with store.snapshot() as db:
            previous = StoredClosedRepository.select(db, repo=repo.github)
            recorded = StoredClosedHead.select(db, repo=repo.github,
                                               where=StoredClosedHead.missing_where)
        floor = max((row.observed_at for row in previous), default=0.0)
        changes = PullRequests().updated_since(repo.github, floor)
        updated = max((floor, *(row.updated_at for row in changes)))
        reader = WorkBranches(repo.checkout)
        (published,) = reader.remote_heads(repo.remote, f'refs/heads/{reader.main}')
        old_tips = tuple(row.main for row in previous)
        closed = tuple(pull for change in changes for pull in change.as_closed())
        Repo.require_commits(reader.checkout, repo.remote, published, *old_tips)
        requested = (*(pull.head for pull in closed), *(row.request.head for row in recorded))
        unservable = Repo.unservable_commits(reader.checkout, repo.remote, *requested)
        # A closed head's branch may be gone: fetched or not, no ref reaches it for the graph.
        reader.history.include(published, *old_tips,
                               *(head for head in requested if head not in unservable))
        for old in old_tips:
            if not Repo.descends(reader.checkout, old, published):
                raise WorkBranchError(f'{repo.github.to_wire()} main rewrote its indexed history')
        # Index main once, then only its new suffix. No shared root across unrelated heads
        # is required, and discovering an older head cannot reopen an indexed prefix.
        fingerprints: dict[str, StoredCommitPatch] = {}
        main_rows = StoredCommitPatch.prepare(store, reader,
                                              reader.commits_since(published, *old_tips),
                                              fingerprints)
        scan = ClosedHeadScan(store, reader, repo.github, published,
                              frozenset(patch for row in main_rows for patch in row.patches),
                              fingerprints, unservable)
        changed = frozenset(row.number for row in changes)
        stale = old_tips != (published,)
        heads = (*(scan.head(row.request, row.closed_at) if stale else row
                   for row in recorded if row.request.pr not in changed),
                 *(scan.head(PullRequestHead(repo.github, pull.number, pull.head), pull.closed_time)
                   for pull in closed))
        return ClosedRepositoryObservation(
            StoredClosedRepository(repo.github, published, updated),
            tuple(row for row in heads if row.missing),
            tuple(fingerprints.values()), scan.new_patches, tuple(scan.diffs.values()))
