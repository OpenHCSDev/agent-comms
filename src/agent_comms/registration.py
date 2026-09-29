"""Thread registration and authority transactions over their determining document."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path

from .catalog_store import ChannelCatalog
from .compaction_publication_lease import publication_identity_fence
from .errors import UnregisteredThreadError
from .goal_history import GoalHistoryEntry, GoalHistoryStore
from .maintenance_barrier import MaintenanceBarrier
from .native_input_owner import RegistryOwner
from .owner_compaction_gate import OwnerCompactionAttestation
from .registration_change import RegistrationChange
from .registry_document import RegistrySnapshot
from .registry_store import RegistryEdit, RegistryStore
from .routing import TurnRouting
from .store_files import _store_lock, file_revision
from .thread_identity import GenerationCounter, TurnId
from .thread_status import RunningThreadStatus, ThreadStatus
from .threads import Thread
from .turn_lease import FinishedTurnFence, TurnLeaseFence

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
        with (
            publication_identity_fence(self.store.path.parent, nonblocking=True)
            if change.changes_identity
            else nullcontext()
        ):
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
        with (
            publication_identity_fence(self.store.path.parent, nonblocking=True),
            self.store.editing() as edit,
        ):
            result = edit.document.rename(name, new_name)
            edit.commit()
            return result

    def fence_idle_owner(self, expected: Thread, *, expected_admission_generation: int) -> int:
        with self.store.editing() as edit:
            result = edit.document.fence_idle_owner(
                expected, expected_admission_generation=expected_admission_generation
            )
            edit.commit()
            return result

    def unregister(self, name: str) -> None:
        with (
            publication_identity_fence(self.store.path.parent, nonblocking=True),
            self.store.editing() as edit,
        ):
            result = edit.document.unregister(name)
            edit.commit()
            return result

    def archive(self, name: str) -> None:
        with (
            publication_identity_fence(self.store.path.parent, nonblocking=True),
            self.store.editing() as edit,
        ):
            result = edit.document.archive(name)
            edit.commit()
            return result

    def begin_delete(self, name: str) -> None:
        with (
            publication_identity_fence(self.store.path.parent, nonblocking=True),
            self.store.editing() as edit,
        ):
            result = edit.document.begin_delete(name)
            edit.commit()
            return result

    def remove(self, name: str) -> tuple[str, ...]:
        with (
            publication_identity_fence(self.store.path.parent, nonblocking=True),
            self.store.editing() as edit,
        ):
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

    def live_owner_with_generation(self, name: str) -> tuple[Thread, int]:
        """Capture an active owner and its persistent incarnation under one lock.

        Unlike a global file revision, an unrelated recipient's claim cannot
        invalidate this owner's attempt. A stop then heartbeat changes its generation
        even if the declaration, PID, and status return to their earlier values.
        """
        with self.store.reading() as document:
            snapshot = document.snapshot()
            owner = RegistryOwner.capture_local(snapshot, name)
            return owner.thread, snapshot.owner_identity(owner.thread.name).generation

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

    @contextmanager
    def guard_owner_compaction(
        self, expected: Thread, receipt: OwnerCompactionAttestation,
    ) -> Iterator[tuple[OwnerCompactionAttestation, int]]:
        """Hold canonical authority through the caller's native mutation.

        Lock order: registry, then native session writer. No registry method
        may be called inside this scope (the lock is not reentrant). A native
        child MUST inherit the yielded descriptor and keep it until exit;
        the caller must bound, terminate and reap it before leaving normally.
        This scope does not validate correction or native session evidence.

        This is NOT a bearer token: the same check must run again at commit
        time under this lock. Anything that moved since the caller captured
        its expectations — owner generation, active turn, goal id/revision/status,
        or liveness — fails closed here. The native session fence (file, leaf,
        disk revision) is echoed unverified; the native writer CAS is the only
        authority for those values.
        """
        Thread.require_declaration(expected)
        with self.store.locked() as authority_fd:
            snapshot = self.store._read_unlocked().snapshot()
            owner = RegistryOwner.capture_local(snapshot, expected.name)
            receipt.require_current(owner, snapshot, expected)
            yield replace(receipt, registry_revision=file_revision(self.store.path)), authority_fd

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
            return document.aliases.get(name, name)

    def aliases_for(self, name: str) -> frozenset[str]:
        with self.store.reading() as document:
            canonical = document.aliases.get(name, name)
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
            canonical = document.aliases.get(name, name)
            thread = document.threads.get(canonical)
            if thread is None:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            return GoalHistoryStore(self.store.path).history(
                thread.created_at, thread.goal, goal_id=goal_id
            )

    def name_reserved(self, name: str) -> bool:
        """Return whether a canonical name or permanent alias occupies text."""
        with self.store.reading() as document:
            return name in document.threads or name in document.aliases

    def last_seen(self, name: str) -> float:
        with self.store.reading() as document:
            name = document.aliases.get(name, name)
            if name not in document.threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            return document.last_seen.get(name, 0.0)

    def require(self, name: str) -> Thread:
        with self.store.reading() as document:
            name = document.aliases.get(name, name)
            if name not in document.threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            return document.threads[name]

    def status(self, name: str) -> ThreadStatus:
        with self.store.reading() as document:
            name = document.aliases.get(name, name)
            if name not in document.statuses:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            return document.statuses[name]

    def all_threads(self) -> Mapping[str, Thread]:
        with self.store.reading() as document:
            return dict(document.threads)

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
            exclude = document.aliases.get(exclude, exclude)
            return [name for name in document.threads if name != exclude]

    def __contains__(self, name: str) -> bool:
        with self.store.reading() as document:
            name = document.aliases.get(name, name)
            return name in document.threads
