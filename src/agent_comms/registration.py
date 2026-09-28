"""Thread registration and authority transactions over their determining document."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .compaction_publication_lease import publication_identity_fence
from .declarations import (
    FinishedTurnFence,
    RegistrySnapshot,
    RelationViolationError,
    Thread,
    TurnLeaseFence,
    TurnRouting,
    UnregisteredThreadError,
    file_revision,
)
from .goal_history import GoalHistoryEntry, GoalHistoryStore
from .maintenance_barrier import MaintenanceBarrier
from .owner_compaction_gate import OwnerCompactionAttestation
from .registry_store import RegistryStore
from .thread_status import RunningThreadStatus, ThreadStatus


class Registration:
    """Own registration transactions; never borrow a Comms or facade's state."""

    def __init__(self, store_path: Path):
        self.store = RegistryStore(store_path)
        self.store.read()

    def register(
        self,
        thread: Thread,
        status: ThreadStatus = RunningThreadStatus(),
        *,
        new_owner: bool = False,
    ) -> None:
        with self.store.editing() as edit:
            change = edit.document.prepare_registration(thread, status, new_owner=new_owner)
            if change.needs_maintenance_admission:
                MaintenanceBarrier(self.store.path).assert_open_unlocked()
            with (
                publication_identity_fence(self.store.path.parent, nonblocking=True)
                if change.changes_identity
                else nullcontext()
            ):
                before = change.previous.goal if change.previous is not None else None
                history = None
                intent = None
                if before != change.thread.goal:
                    history = GoalHistoryStore(self.store.path)
                    intent = history.begin(change.thread.created_at, before, change.thread.goal)
                edit.document.apply_registration(change)
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
            canonical = document.aliases.get(name, name)
            owner = document.threads.get(canonical)
            status = document.statuses.get(canonical)
            generation = document.owners.generations.get(canonical)
            if (
                owner is None
                or status is None
                or not status.active
                or owner.pid != os.getpid()
                or not owner.role.executable
                or not document.generation_metadata_present
                or generation is None
                or (
                    owner is not None
                    and owner.active_turn is not None
                    and not owner.active_turn.current(
                        document.admissions.generations[canonical], owner.turn_generation
                    )
                )
            ):
                raise RelationViolationError("live owner is stopped or unavailable")
            return owner, generation

    def live_owner_with_admission(self, name: str) -> tuple[Thread, int]:
        """Read the durable process admission, independent of metadata revisions."""
        with self.store.reading() as document:
            canonical = document.aliases.get(name, name)
            owner = document.threads.get(canonical)
            status = document.statuses.get(canonical)
            generation = document.admissions.generations.get(canonical)
            if (
                owner is None
                or status is None
                or not status.active
                or owner.pid != os.getpid()
                or not owner.role.executable
                or not document.generation_metadata_present
                or generation is None
                or (
                    owner is not None
                    and owner.active_turn is not None
                    and not owner.active_turn.current(generation, owner.turn_generation)
                )
            ):
                raise RelationViolationError("live owner is stopped or unavailable")
            return owner, generation

    def claim_live_turn_with_admission(
        self, expected: Thread, turn_id: str, *, expected_generation: int
    ) -> tuple[Thread, int]:
        """Claim a turn against stable owner authority under the registry lock."""
        if (
            type(expected) is not Thread
            or type(turn_id) is not str
            or not 0 < len(turn_id) <= 128
            or type(expected_generation) is not int
            or expected_generation < 1
        ):
            raise ValueError("live owner turn requires exact admission and bounded ID")
        with self.store.editing() as edit:
            document = edit.document
            current = document.threads.get(expected.name)
            status = document.statuses.get(expected.name)
            if (
                not document.generation_metadata_present
                or document.admissions.generations.get(expected.name) != expected_generation
                or current is None
                or status is None
                or not status.active
                or current.pid != os.getpid()
                or not current.role.executable
                or current.active_turn is not None
                or current.goal != expected.goal
                or (current.name, current.created_at, current.pid, current.role, current.worktree)
                != (
                    expected.name,
                    expected.created_at,
                    expected.pid,
                    expected.role,
                    expected.worktree,
                )
            ):
                raise RelationViolationError("live owner stopped or changed before turn claim")
            self._assert_maintenance_open_unlocked()
            claimed, _owner_generation = document.claim_turn(current, turn_id, None)
            edit.commit()
            return claimed, expected_generation

    def claim_live_turn_with_generation(
        self,
        expected: Thread,
        turn_id: str,
        *,
        expected_owner_generation: int,
        routing: TurnRouting | None = None,
    ) -> tuple[Thread, int]:
        """Atomically claim a fresh turn and return its turn with stable owner generation.

        Never sample the generation in a second read: an owner may stop and register
        the same declaration between that read and the claim's return.
        """
        if (
            type(expected) is not Thread
            or type(turn_id) is not str
            or not 0 < len(turn_id) <= 128
            or type(expected_owner_generation) is not int
            or expected_owner_generation < 1
        ):
            raise ValueError("live owner turn requires exact identity, generation and bounded ID")
        with self.store.editing() as edit:
            document = edit.document
            current = document.threads.get(expected.name)
            status = document.statuses.get(expected.name)
            if (
                not document.generation_metadata_present
                or document.owners.generations.get(expected.name) != expected_owner_generation
                or current != expected
                or status is None
                or not status.active
                or current is None
                or current.pid != os.getpid()
                or not current.role.executable
                or current.active_turn is not None
            ):
                raise RelationViolationError("live owner stopped or changed before turn claim")
            self._assert_maintenance_open_unlocked()
            result = document.claim_turn(current, turn_id, routing)
            edit.commit()
            return result

    def attest_owner_compaction(
        self,
        expected: Thread,
        expected_owner_generation: int,
        turn_id: str,
        *,
        expected_goal_id: str | None,
        expected_goal_revision: int | None,
        correction_revision: int,
        session_file: str,
        session_leaf: str,
        session_revision: str,
    ) -> OwnerCompactionAttestation:
        """Return an audit snapshot, NOT authority for a later native mutation."""
        with self.guard_owner_compaction(
            expected,
            expected_owner_generation,
            turn_id,
            expected_goal_id=expected_goal_id,
            expected_goal_revision=expected_goal_revision,
            correction_revision=correction_revision,
            session_file=session_file,
            session_leaf=session_leaf,
            session_revision=session_revision,
        ) as (attestation, _):
            return attestation

    @contextmanager
    def guard_owner_compaction(
        self,
        expected: Thread,
        expected_owner_generation: int,
        turn_id: str,
        *,
        expected_goal_id: str | None,
        expected_goal_revision: int | None,
        correction_revision: int,
        session_file: str,
        session_leaf: str,
        session_revision: str,
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
        if (
            type(expected) is not Thread
            or type(expected_owner_generation) is not int
            or expected_owner_generation < 1
            or type(turn_id) is not str
            or not 0 < len(turn_id) <= 128
            or ((expected_goal_id is None) != (expected_goal_revision is None))
            or (
                expected_goal_id is not None
                and (
                    type(expected_goal_id) is not str
                    or not expected_goal_id
                    or type(expected_goal_revision) is not int
                    or expected_goal_revision < 0
                )
            )
            or type(correction_revision) is not int
            or correction_revision < 0
            or type(session_file) is not str
            or not session_file
            or type(session_leaf) is not str
            or not session_leaf
            or type(session_revision) is not str
            or not session_revision
        ):
            raise ValueError("owner compaction attestation requires bounded exact expectations")
        with self.store.locked() as authority_fd:
            document = self.store._read_unlocked()
            canonical = document.aliases.get(expected.name, expected.name)
            owner = document.threads.get(canonical)
            status = document.statuses.get(canonical)
            generation = document.owners.generations.get(canonical)
            goal = owner.goal if owner is not None else None
            if (
                not document.generation_metadata_present
                or owner is None
                or status is None
                or not status.active
                or owner != expected
                or generation != expected_owner_generation
                or owner.pid != os.getpid()
                or not owner.role.executable
                or owner.active_turn is None
                or owner.active_turn.id != turn_id
                or not owner.active_turn.current(
                    document.admissions.generations[canonical], owner.turn_generation
                )
                or (goal.id if goal is not None else None) != expected_goal_id
                or (goal.revision if goal is not None else None) != expected_goal_revision
            ):
                raise RelationViolationError(
                    "canonical owner attestation unavailable for compaction commit"
                )
            from .owner_compaction_gate import OwnerCompactionAttestation

            yield (
                OwnerCompactionAttestation(
                    thread=owner.name,
                    owner_epoch=generation,
                    turn_id=turn_id,
                    goal_id=goal.id if goal is not None else None,
                    goal_revision=goal.revision if goal is not None else None,
                    correction_revision=correction_revision,
                    session_file=session_file,
                    session_leaf=session_leaf,
                    session_revision=session_revision,
                    registry_revision=file_revision(self.store.path),
                ),
                authority_fd,
            )

    def _assert_maintenance_open_unlocked(self) -> None:
        from .maintenance_barrier import MaintenanceBarrier

        MaintenanceBarrier(self.store.path).assert_open_unlocked()

    def claim_local_turn(
        self, name: str, turn_id: str, *, routing: TurnRouting | None = None
    ) -> tuple[Thread, int]:
        """Atomic local begin-turn, never reviving a stopped or replaced owner.

        A legacy unmarked registry can be migrated under the same lock as the
        claim. Marked private roots must not recreate missing generation metadata:
        an old writer may have stripped it during an unsafe cutover.
        """
        if type(turn_id) is not str or not 0 < len(turn_id) <= 128:
            raise ValueError("live owner turn requires a bounded ID")
        with self.store.editing() as edit:
            document = edit.document
            canonical = document.aliases.get(name, name)
            current = document.threads.get(canonical)
            status = document.statuses.get(canonical)
            if (
                current is None
                or status is None
                or not status.active
                or current.pid != os.getpid()
                or not current.role.executable
                or current.active_turn is not None
            ):
                raise RelationViolationError("live owner is stopped or unavailable")
            if not document.generation_metadata_present:
                document.generation_metadata_present = True
            self._assert_maintenance_open_unlocked()
            result = document.claim_turn(current, turn_id, routing)
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
        from .goal_history import GoalHistoryStore

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
