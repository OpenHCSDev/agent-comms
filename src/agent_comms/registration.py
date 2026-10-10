"""Thread registration and authority transactions over their determining document."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from .catalog_store import ChannelCatalog
from .goal_history import GoalHistoryEntry, GoalHistoryStore
from .maintenance_barrier import MaintenanceBarrier
from .native_input_owner import RegistryOwner
from .registration_change import RegistrationChange
from .registry_document import RegistrySnapshot
from .registry_store import RegistryEdit, RegistryEntryRevision, RegistryStore
from .routing import TurnRouting
from .store_files import _store_lock
from .thread_identity import GenerationCounter, TurnId
from .thread_status import RunningThreadStatus, ThreadStatus
from .threads import Thread
from .turn_lease import FinishedTurnFence, TurnLeaseFence, TurnState
from .turn_phase import TurnPhase

_RUNNING_STATUS = RunningThreadStatus()


class Registration:
    """Own registration transactions; never borrow a Comms or facade's state."""

    def __init__(self, store_path: Path):
        self.store = RegistryStore(store_path)
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

    def attach_native_session(self, original: RegistryOwner, session_file: str) -> RegistryOwner:
        """Publish an observed native source under its original executable lease.

        Registry admission checks own the determining facts. A changing progress
        phase is retained from the locked document, not compared to an old view.
        Native session identity and context proof remain with their producers.
        """
        with _store_lock(self.store.path.parent / "wire", shared=True), self.store.editing() as edit:
            change = edit.document.prepare_native_source(original, session_file)
            self._commit_registration(edit, change)
            # The document owns the next turn's selection. Publishing an
            # observed source must not rewrite this admitted turn's settings.
            observed = replace(change.installed_thread,
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

    def _declare_in(self, edit: RegistryEdit, thread: Thread, status: ThreadStatus) -> Thread:
        root = self.store.path.parent
        catalog = ChannelCatalog(root / ChannelCatalog.filename)
        change = edit.document.prepare_declaration(thread, status)
        with catalog.editing() as channels:
            for tag in change.thread.tags - channels.all_tags(edit.document.threads):
                channels.require_available_tag_name(tag, edit.document.threads)
            self._commit_registration(edit, change)
            channels.remember_tags(change.thread.tags, change.thread.created_at)
        return change.installed_thread

    def _commit_registration(self, edit: RegistryEdit, change: RegistrationChange) -> None:
        if change.needs_maintenance_admission:
            MaintenanceBarrier(self.store.path).assert_open_unlocked()
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
                    thread, expected_admission_generation=generation
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

    def transition_turn(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        with self.store.editing() as edit:
            effects = edit.document.transition_turn(lease, phase)
            edit.commit()
            return effects

    def observe_native_phase(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        """Interpret the observation against this same locked original turn."""
        with self.store.editing() as edit:
            effects = edit.document.observe_native_phase(lease, phase)
            edit.commit()
            return effects

    def live_owner_with_admission(self, name: str) -> tuple[Thread, int]:
        """Read the durable process admission, independent of metadata revisions."""
        with self.store.reading() as document:
            owner = RegistryOwner.capture_local(document.snapshot(), name)
            return owner.thread, owner.admission_generation

    def lease_live_turn_with_admission(
        self, expected: Thread, turn_id: str, *, expected_generation: int
    ) -> tuple[Thread, int]:
        """Claim a turn against stable admission, allowing unrelated metadata changes."""
        Thread.require_declaration(expected)
        turn = TurnId.for_registration(turn_id)
        generation = GenerationCounter.require_positive(expected_generation)
        with self.store.editing() as edit:
            owner = RegistryOwner.capture_local(edit.document.snapshot(), expected.name)
            owner.require_claim(expected, generation)
            self._assert_maintenance_open_unlocked()
            leased, _ = edit.document.lease_turn(owner.thread, turn.value, None)
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
            owner.thread.require_idle()
            self._assert_maintenance_open_unlocked()
            result = edit.document.lease_turn(owner.thread, turn.value, routing)
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
            owner.thread.require_idle()
            self._assert_maintenance_open_unlocked()
            result = edit.document.lease_turn(owner.thread, turn.value, routing)
            edit.commit()
            return result

    def canonical_name(self, name: str) -> str:
        with self.store.reading() as document:
            return document.canonical_name(name)

    def aliases_for(self, name: str) -> frozenset[str]:
        with self.store.reading() as document:
            canonical = document.canonical_name(name)
            return frozenset(
                {
                    canonical,
                    *(alias for alias, target in document.aliases.items() if target == canonical),
                }
            )

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

    def name_reserved(self, name: str) -> bool:
        """Return whether a canonical name or permanent alias occupies text."""
        with self.store.reading() as document:
            return name in document.threads or name in document.aliases

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
