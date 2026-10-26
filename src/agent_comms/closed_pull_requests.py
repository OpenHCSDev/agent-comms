"""Prepare closed-work answers outside the heartbeat, then publish one committed snapshot."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .head_review import PullRequestHead
from .message_bus import Watch
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


class ClosedPullRequestWatch(Watch):
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
        (published,) = reader.remote_heads('origin', f'refs/heads/{reader.main}')
        old_tips = tuple(row.main for row in previous)
        closed = tuple(pull for change in changes for pull in change.as_closed())
        compared = tuple(dict.fromkeys((*old_tips, *(pull.head for pull in closed),
                                        *(row.request.head for row in recorded))))
        reader.require_commits('origin', published, *compared)
        bases = reader.common_bases(published, compared)
        for old in old_tips:
            if not bases[old].ancestor(old):
                raise WorkBranchError(f'{repo.github.to_wire()} main rewrote its indexed history')
        # Index main once, then only its new suffix. No shared root across unrelated heads
        # is required, and discovering an older head cannot reopen an indexed prefix.
        added = reader.commits_since(published, *old_tips)
        fingerprints: dict[str, StoredCommitPatch] = {}
        main_rows = StoredCommitPatch.prepare(store, reader, added, fingerprints)
        new_patches = frozenset(patch for row in main_rows for patch in row.patches)
        changed = frozenset(row.number for row in changes)
        heads = {row.request.pr: row for row in recorded if row.request.pr not in changed}
        diffs: dict[tuple[str, str], StoredHeadDiff] = {}
        for pull in closed:
            request = PullRequestHead(repo.github, pull.number, pull.head)
            heads[pull.number] = StoredClosedHead(
                request, bases[pull.head], pull.closed_time,
                reader.commits_since(pull.head, published))
        prepared: list[StoredClosedHead] = []
        for row in heads.values():
            if row.missing and (row.request.pr in changed or old_tips != (published,)):
                row = replace(row, base=bases[row.request.head])
                for base in row.base.shared():
                    pair = base, row.request.head
                    if pair not in diffs:
                        diffs[pair] = StoredHeadDiff.prepare(store, reader, *pair)
                    diff = diffs[pair]
                    candidates = StoredCommitPatch.prepare(store, reader, diff.commits, fingerprints)
                    with store.snapshot() as db:
                        whole_landed = StoredMainPatch.contains_all(
                            db, repo.github, diff.patches - new_patches)
                        missing = tuple(commit.commit for commit in candidates
                                        if not StoredMainPatch.contains_all(
                                            db, repo.github, commit.patches - new_patches))
                    row = replace(row, missing=() if row.base.ancestor(row.request.head)
                                  or whole_landed else missing)
            if row.missing:
                prepared.append(row)
        return ClosedRepositoryObservation(
            StoredClosedRepository(repo.github, published, updated), tuple(prepared),
            tuple(fingerprints.values()), new_patches, tuple(diffs.values()))
