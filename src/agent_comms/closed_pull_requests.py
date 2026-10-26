"""Prepare closed-work answers outside the heartbeat, then publish one committed snapshot."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .commander.publication_rules import ClosedUnmergedPullRequest
from .head_review import PullRequestHead
from .message_bus import WatchPass
from .repo import Repo
from .todos import (
    PullRequests, StoredClosedHead, StoredClosedRepository, StoredCommitPatch,
    StoredHeadDiff, StoredMainPatch,
)
from .work_branches import WorkBranchError, WorkBranches

if TYPE_CHECKING:
    from .comms import Comms
    from .todos import TodoStore


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


class ClosedPullRequestWatch(WatchPass[tuple[ClosedRepositoryObservation, ...]]):
    """The forge poll owns close/reopen observations; a frame only reads its prepared rows."""

    def observe(self, comms: Comms) -> tuple[ClosedRepositoryObservation, ...]:
        since = time.time() - ClosedUnmergedPullRequest.window
        return tuple(self.observe_repository(comms.goals.todos, repo, since)
                     for repo in comms.registry.snapshot().commander_project.unique_repositories())

    def pass_body(self, comms: Comms, observed: tuple[ClosedRepositoryObservation, ...]) -> None:
        for item in observed:
            item.commit(comms.goals.todos)

    @staticmethod
    def observe_repository(store: TodoStore, repo: Repo, since: float) -> ClosedRepositoryObservation:
        with store.snapshot() as db:
            previous = StoredClosedRepository.select(db, repo=repo.github)
            recorded = StoredClosedHead.select(db, repo=repo.github,
                                               where='{closed_at}>=?', parameters=(since,))
        floor = max((since, *(row.observed_at for row in previous)))
        changes = PullRequests().updated_since(repo.github, floor)
        updated = max((floor, *(row.updated_at for row in changes)))
        reader = WorkBranches(repo.checkout)
        (published,) = reader.remote_heads('origin', f'refs/heads/{reader.main}')
        old_tips = tuple(row.main for row in previous)
        old_roots = tuple(row.oldest for row in previous)
        closed = tuple(pull for change in changes for pull in change.as_closed()
                       if pull.closed_time >= since)
        reader.require_commits('origin', published, *old_tips, *old_roots,
                               *(pull.head for pull in closed))
        for old in old_tips:
            if reader.common_commit(old, published) != old:
                raise WorkBranchError(f'{repo.github.to_wire()} main rewrote its indexed history')
        oldest = reader.common_commit(published, published, *old_roots,
                                      *(pull.head for pull in closed))
        # Advance only the suffix. A newly discovered older fork backfills only the prefix
        # that the index has never seen; its individual commit fingerprints are immutable.
        added = reader.commits_since(published, *(old_tips or (oldest,)))
        backfill = tuple(commit for old in old_roots if old != oldest
                         for commit in reader.commits_since(old, oldest))
        fingerprints: dict[str, StoredCommitPatch] = {}
        main_rows = StoredCommitPatch.prepare(store, reader,
                                              tuple(dict.fromkeys((*added, *backfill))), fingerprints)
        new_patches = frozenset(patch for row in main_rows for patch in row.patches)
        changed = frozenset(row.number for row in changes)
        heads = {row.request.pr: row for row in recorded if row.request.pr not in changed}
        diffs: dict[tuple[str, str], StoredHeadDiff] = {}
        for pull in closed:
            request = PullRequestHead(repo.github, pull.number, pull.head)
            base = next((row.base for row in recorded if row.request == request),
                        reader.common_commit(published, pull.head))
            heads[pull.number] = StoredClosedHead(
                request, base, pull.closed_time,
                next((row.missing for row in recorded if row.request == request),
                     reader.commits_since(pull.head, base)))
        prepared: list[StoredClosedHead] = []
        for row in heads.values():
            if row.missing and (row.request.pr in changed or old_tips != (published,)):
                pair = row.base, row.request.head
                if pair not in diffs:
                    diffs[pair] = StoredHeadDiff.prepare(store, reader, *pair)
                diff = diffs[pair]
                candidates = StoredCommitPatch.prepare(store, reader, diff.commits, fingerprints)
                ancestor = reader.common_commit(published, row.request.head) == row.request.head
                with store.snapshot() as db:
                    whole_landed = StoredMainPatch.contains_all(
                        db, repo.github, diff.patches - new_patches)
                    missing = tuple(commit.commit for commit in candidates
                                    if not StoredMainPatch.contains_all(
                                        db, repo.github, commit.patches - new_patches))
                row = replace(row, missing=() if ancestor or whole_landed else missing)
            prepared.append(row)
        return ClosedRepositoryObservation(
            StoredClosedRepository(repo.github, published, oldest, updated), tuple(prepared),
            tuple(fingerprints.values()), new_patches, tuple(diffs.values()))
