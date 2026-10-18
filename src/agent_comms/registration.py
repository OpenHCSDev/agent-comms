"""Thread registration and authority transactions over their determining document."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .catalog_store import ChannelCatalog
from .child_process import NoProcess, ProcessIdentity
from .errors import RelationViolationError
from .goal_history import GoalHistoryEntry, GoalHistoryStore
from .maintenance_barrier import MaintenanceBarrier
from .native_input_owner import RegistryOwner
from .registration_change import RegistrationChange
from .registry_document import RegistrySnapshot
from .registry_store import RegistryEdit, RegistryEntryRevision, RegistryStore
from .routing import TurnRouting
from .store_files import SharedLock, _store_lock
from .thread_identity import GenerationCounter, TurnId
from .thread_status import (
    ConnectedThreadStatus, NoBridgeThreadStatus, RunningThreadStatus, ThreadStatus,
)
from .threads import Thread
from .storage_rules import StorageRule
from .turn_lease import FinishedTurnFence, TurnLeaseFence

_RUNNING_STATUS = RunningThreadStatus()


class Registration:
    """Own registration transactions; never borrow a Comms or facade's state."""

    def __init__(self, store_path: Path):
        self.store = RegistryStore(store_path)
        self.storage_rule = StorageRule.load(store_path.parent)
        self.store.read()

    def register(
        self,
        thread: Thread,
        status: ThreadStatus = _RUNNING_STATUS,
        *,
        new_owner: bool = False,
    ) -> None:
        with self.store.editing() as edit:
            change = edit.document.prepare_registration(thread, status, new_owner=new_owner)
            self._commit_registration(edit, change)

    @contextmanager
    def external_process(self, name: str, process: ProcessIdentity):
        """The CLI bridge owns its registered endpoint until that same process leaves."""
        with self.store.editing() as edit:
            thread = edit.document.require(name)
            thread.execution.require_external()
            if thread.process_alive and thread.process_identity != process:
                raise RelationViolationError("Another live process owns this CLI endpoint.")
            bound = replace(thread, process_identity=process)
            self._commit_registration(edit, edit.document.prepare_registration(
                bound, ConnectedThreadStatus(), new_owner=True))
            owner = edit.document.entry(bound.name).owner_identity
        try:
            yield owner.incarnation
        finally:
            with self.store.editing() as edit:
                entry = edit.document.entry_if_present(owner.incarnation.name)
                if entry is not None and entry.owned_by(owner, process):
                    self._commit_registration(edit, edit.document.prepare_registration(
                        replace(entry.thread, process_identity=NoProcess()), NoBridgeThreadStatus(),
                        new_owner=False))

    def attach_native_session(self, original: RegistryOwner, session_file: str) -> RegistryOwner:
        """Publish an observed native source under its original executable lease.

        Registry permission checks own the determining facts. A changing progress
        phase is retained from the locked document, not compared to an old view.
        Native session identity and context proof remain with their producers.
        """
        with _store_lock(self.store.path.parent / "wire", mode=SharedLock()), self.store.editing() as edit:
            change = edit.document.prepare_native_source(original, session_file)
            change = self._commit_registration(edit, change)
            # The document owns the next turn's selection. Publishing an
            # observed source must not rewrite this authorized turn's settings.
            observed = replace(change.thread,
                worktree=original.thread.worktree, model=original.thread.model,
                thinking_level=original.thread.thinking_level)
            return replace(original, thread=observed)

    def declare(self, thread: Thread, status: ThreadStatus = _RUNNING_STATUS) -> Thread:
        """Operational declaration and channel provenance use one locked current owner.

        Internal register remains exact state replacement; worker/public declaration
        preserves omitted provenance and a current executor before applying the same
        registration effects. No pre-read can authorize a stale owner replacement.
        """
        with _store_lock(self.store.path.parent / "wire"):
            return self._declare_unlocked(thread, status)

    def _declare_unlocked(self, thread: Thread, status: ThreadStatus = _RUNNING_STATUS) -> Thread:
        """Cross-store callers already holding wire use the same registry transaction."""
        with self.store.editing() as edit:
            return self._declare_in(edit, thread, status)

    def _claim_unlocked(self, thread: Thread) -> Thread:
        """The wire caller holds inbox authority; namespace allocation stays registry-owned."""
        with self.store.editing() as edit:
            requested = thread.for_claim(edit.document.claim_name(thread.name))
            return self._declare_in(edit, requested, RunningThreadStatus())

    @staticmethod
    def _prepare_declaration(document, channels, thread: Thread, status: ThreadStatus):
        """Derive one declaration after checking its complete namespace."""
        change = document.prepare_declaration(thread, status)
        for tag in change.thread.tags - channels.all_tags(document.threads):
            channels.require_available_tag_name(tag, document.threads)
        return change

    def require_declaration_available(
        self, thread: Thread, status: ThreadStatus = _RUNNING_STATUS,
    ) -> None:
        """Refuse a known-invalid declaration before its producer performs side effects."""
        catalog = ChannelCatalog.at(self.store.path.parent)
        with self.store.reading() as document, catalog.reading() as channels:
            self._prepare_declaration(document, channels, thread, status)

    def _declare_in(self, edit: RegistryEdit, thread: Thread, status: ThreadStatus) -> Thread:
        root = self.store.path.parent
        catalog = ChannelCatalog.at(root)
        with catalog.editing() as channels:
            change = self._prepare_declaration(edit.document, channels, thread, status)
            change = self._commit_registration(edit, change)
            channels.remember_tags(change.thread.tags, change.thread.created_at)
        return change.thread

    def _commit_registration(self, edit: RegistryEdit, change: RegistrationChange) -> RegistrationChange:
        change.thread.role.require_maintenance_open(
            MaintenanceBarrier(self.store.path), change.claims_owner
        )
        before = change.prior_goal
        history = None
        intent = None
        if before != change.thread.goal:
            history = GoalHistoryStore(self.store.path)
            intent = history.begin(change.thread.created_at, before, change.thread.goal)
        change.apply(edit.document)
        edit.commit()
        if history is not None and intent is not None:
            history.commit(intent)
        return change

    def new_thread(self, name, tags, worktree, *args, kind: type[Thread] = Thread, **values) -> Thread:
        return kind(name, tags, worktree, *args,
                      storage_roots=self.storage_rule.initial_roots(name), **values)

    def decide_storage(self, name, effects):
        current = self.require(name)
        for effect in effects:
            effect.check(self.storage_rule, current)

    def restore_stopped(self, source: RegistrySnapshot, names: Sequence[str]) -> tuple[str, ...]:
        with self.store.editing() as edit:
            result = edit.document.restore_stopped(source, names)
            edit.commit()
            return result

    def rename(self, name: str, new_name: str) -> tuple[str, str]:
        with self.store.editing() as edit:
            result = edit.document.rename(name, new_name)
            edit.commit()
            return result

    def fence_idle_owners(
        self, selected: Sequence[tuple[Thread, int]]
    ) -> tuple[tuple[Thread, int], ...]:
        """Commit all original idle owner fences together, or change none."""
        with self.store.editing() as edit:
            fenced = tuple(
                (thread, edit.document.fence_idle_owner(
                    thread, expected_claimed_generation=generation
                ))
                for thread, generation in selected
            )
            edit.commit()
            return fenced

    def unregister(self, name: str) -> None:
        with self.store.editing() as edit:
            result = edit.document.unregister(name)
            edit.commit()
            return result

    def archive(self, name: str) -> None:
        with self.store.editing() as edit:
            result = edit.document.archive(name)
            edit.commit()
            return result

    def begin_delete(self, name: str) -> None:
        with self.store.editing() as edit:
            result = edit.document.begin_delete(name)
            edit.commit()
            return result

    def archive_originals(self, originals: Sequence[Thread]) -> None:
        with self.store.editing() as edit:
            edit.document.archive_originals(originals)
            edit.commit()

    def begin_delete_originals(self, originals: Sequence[Thread]) -> None:
        with self.store.editing() as edit:
            edit.document.begin_delete_originals(originals)
            edit.commit()

    def remove_originals(self, originals: Sequence[Thread]) -> dict[str, tuple[str, ...]]:
        with self.store.editing() as edit:
            detached = edit.document.remove_originals(originals)
            edit.commit()
            return detached

    def change_tag(self, originals: Sequence[Thread], tag: str, replacement: str | None) -> None:
        with self.store.editing() as edit:
            edit.document.change_tag(originals, tag, replacement)
            edit.commit()

    def remove(self, name: str) -> tuple[str, ...]:
        with self.store.editing() as edit:
            result = edit.document.remove(name)
            edit.commit()
            return result

    def heartbeat(self, name: str) -> None:
        with self.store.editing() as edit:
            result = edit.document.heartbeat(name)
            edit.commit()
            return result

    def release_turn(self, lease: TurnLeaseFence) -> tuple[bool, FinishedTurnFence | None]:
        with self.store.editing() as edit:
            result = edit.document.release_turn(lease)
            edit.commit()
            return result

    def live_owner_with_claim(self, name: str) -> tuple[Thread, int]:
        """Read the stored process input generation, independent of metadata revisions."""
        with self.store.reading() as document:
            owner = RegistryOwner.capture_local(document.snapshot(), name)
            return owner.thread, owner.claimed_generation

    def lease_live_turn_with_claim(
        self, expected: Thread, turn_id: str, *, expected_generation: int
    ) -> tuple[Thread, int]:
        """Claim a turn against the input generation, allowing unrelated metadata changes."""
        Thread.require_declaration(expected)
        turn = TurnId.for_registration(turn_id)
        generation = GenerationCounter.require_positive(expected_generation)
        with self.store.editing() as edit:
            owner = RegistryOwner.capture_local(edit.document.snapshot(), expected.name)
            owner.require_claim(expected, generation)
            self._assert_maintenance_open_unlocked()
            leased, _ = edit.lease_turn(owner.thread, turn.value, None)
            edit.commit()
            return leased, generation

    def lease_live_turn_with_generation(
        self,
        expected: Thread,
        turn_id: str,
        *,
        expected_owner_generation: int,
        routing: TurnRouting | None = None,
    ) -> tuple[Thread, int]:
        """Claim against the exact captured declaration and stable owner generation."""
        Thread.require_declaration(expected)
        turn = TurnId.for_registration(turn_id)
        generation = GenerationCounter.require_positive(expected_owner_generation)
        with self.store.editing() as edit:
            snapshot = edit.document.snapshot()
            owner = RegistryOwner.capture_local(snapshot, expected.name)
            owner.require_exact(snapshot, expected, generation)
            owner.thread.turn_record.read().require_idle()
            self._assert_maintenance_open_unlocked()
            result = edit.lease_turn(owner.thread, turn.value, routing)
            edit.commit()
            return result

    def _assert_maintenance_open_unlocked(self) -> None:
        from .maintenance_barrier import MaintenanceBarrier

        MaintenanceBarrier(self.store.path).assert_open_unlocked()

    def lease_local_turn(
        self, name: str, turn_id: str, *, routing: TurnRouting | None = None
    ) -> tuple[Thread, int]:
        """Atomic local begin-turn, never reviving a stopped or replaced owner."""
        turn = TurnId.for_registration(turn_id)
        with self.store.editing() as edit:
            owner = RegistryOwner.capture_local(edit.document.snapshot(), name)
            owner.thread.turn_record.read().require_idle()
            self._assert_maintenance_open_unlocked()
            result = edit.lease_turn(owner.thread, turn.value, routing)
            edit.commit()
            return result

    def canonical_name(self, name: str) -> str:
        with self.store.reading() as document:
            return document.canonical_name(name)

    def goal_history(
        self, name: str, *, goal_id: str | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read this owner's recorded transitions, reconciling crash cuts first."""

        with self.store.locked():
            document = self.store._read_unlocked()
            thread = document.require(name)
            return GoalHistoryStore(self.store.path).history(
                thread.created_at, thread.goal, goal_id=goal_id
            )

    def name_taken(self, name: str, owner: str | None = None) -> bool:
        """Whether text is taken for ``owner``, read from one store revision."""
        with self.store.reading() as document:
            return document.taken(name, owner)

    def last_seen(self, name: str) -> float:
        with self.store.reading() as document:
            return document.seen_at(name)

    def require(self, name: str) -> Thread:
        with self.store.reading() as document:
            return document.require(name)

    def status(self, name: str) -> ThreadStatus:
        with self.store.reading() as document:
            return document.status(name)

    def all_threads(self) -> Mapping[str, Thread]:
        with self.store.reading() as document:
            return dict(document.threads)

    def entry(self, name: str) -> RegistryEntryRevision:
        """This thread's registry entry and the revision it was read from.

        Other threads' declarations are not decoded.
        """
        return self.store.read_entry(name)

    def snapshot(self) -> RegistrySnapshot:
        """Read related declarations and statuses from exactly one store revision."""
        with self.store.reading() as document:
            return document.snapshot()

    def active_threads(self) -> Mapping[str, Thread]:
        with self.store.reading() as document:
            return {
                name: t for name, t in document.threads.items() if document.statuses[name].active
            }

    def peers(self, exclude: str) -> Sequence[str]:
        with self.store.reading() as document:
            exclude = document.canonical_name(exclude)
            return [name for name in document.threads if name != exclude]

    def __contains__(self, name: str) -> bool:
        with self.store.reading() as document:
            name = document.canonical_name(name)
            return name in document.threads
