"""One registry document: thread facts, identity domains and lifecycle behavior."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import FieldCodec
from .routing import TurnRouting
from .thread_identity import GenerationCounter, OwnerIdentity
from .thread_status import (
    ArchivedThreadStatus,
    RunningThreadStatus,
    StoppedThreadStatus,
    ThreadStatus,
)
from .threads import Thread
from .turn_lease import ActiveTurn, FinishedTurnFence, TurnLeaseFence


@dataclass(frozen=True, slots=True)
class RegistrationChange:
    previous: Thread | None
    previous_status: ThreadStatus | None
    thread: Thread
    status: ThreadStatus
    new_owner: bool

    @property
    def needs_maintenance_admission(self) -> bool:
        return self.thread.role.executable and (
            self.new_owner
            or (self.previous is None and self.thread.pid > 0 and self.status.active)
            or (self.previous is not None and self.previous.pid != self.thread.pid)
            or (
                self.previous_status is not None
                and not self.previous_status.active
                and self.status.active
            )
        )

    @property
    def changes_identity(self) -> bool:
        previous, thread = self.previous, self.thread
        return previous is not None and (
            self.new_owner
            or previous.created_at != thread.created_at
            or previous.pid != thread.pid
            or previous.session_file != thread.session_file
            or previous.worktree != thread.worktree
            or previous.role != thread.role
            or (
                self.previous_status is not None and self.previous_status.changes_owner(self.status)
            )
        )


@dataclass(slots=True)
class RegistryDocument:
    threads: dict[str, Thread] = field(default_factory=dict)
    statuses: dict[str, ThreadStatus] = field(default_factory=dict)
    last_seen: dict[str, float] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    owners: GenerationCounter = field(default_factory=GenerationCounter)
    admissions: GenerationCounter = field(default_factory=GenerationCounter)

    def copy(self) -> RegistryDocument:
        return replace(
            self,
            threads=dict(self.threads),
            statuses=dict(self.statuses),
            last_seen=dict(self.last_seen),
            aliases=dict(self.aliases),
            owners=GenerationCounter(self.owners.counter, dict(self.owners.generations)),
            admissions=GenerationCounter(
                self.admissions.counter, dict(self.admissions.generations)
            ),
        )

    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(
            dict(self.threads),
            dict(self.statuses),
            dict(self.last_seen),
            dict(self.aliases),
            dict(self.owners.generations),
            dict(self.admissions.generations),
        )

    @classmethod
    def from_wire(cls, raw: dict) -> RegistryDocument:
        try:
            document = FieldCodec.decode(cls, raw)
            # Creation identity is supplied by storage, never synthesized from
            # the clock or a session file during a read.
            if any("created_at" not in record for record in raw["threads"].values()):
                raise ValueError("missing stored creation identity")
            names = set(document.threads)
            if names != set(document.statuses) or names != set(document.last_seen):
                raise ValueError("thread presence does not match declarations")
            if not names <= document.owners.generations.keys():
                raise ValueError("missing owner generation")
            if not names <= document.admissions.generations.keys():
                raise ValueError("missing admission generation")
            if any(name != thread.name for name, thread in document.threads.items()):
                raise ValueError("thread name differs from its declaration")
            if any(
                target not in names or alias in names for alias, target in document.aliases.items()
            ):
                raise ValueError("alias has no exclusive retained owner")
            return document
        except (ValueError, TypeError, KeyError) as error:
            raise RelationViolationError(f"Invalid registry document: {error}") from error

    def prepare_registration(
        self, thread: Thread, status: ThreadStatus, *, new_owner: bool
    ) -> RegistrationChange:
        if thread.name in self.aliases:
            raise RelationViolationError(
                f"Thread name {thread.name!r} is a permanent alias and cannot be reused."
            )
        self.statuses.get(thread.name, RunningThreadStatus()).require_mutable(thread.name)
        previous = self.threads.get(thread.name)
        previous_status = self.statuses.get(thread.name)
        if previous:
            thread = replace(thread, created_at=previous.created_at)
            if thread.tags != previous.tags:
                if previous.channel_scope_generation >= (1 << 63) - 1:
                    raise RelationViolationError("Channel scope generation exhausted")
                thread = replace(
                    thread,
                    channel_scope_generation=previous.channel_scope_generation + 1,
                )
            elif thread.channel_scope_generation != previous.channel_scope_generation:
                # A metadata writer cannot erase or forge channel scope history.
                thread = replace(thread, channel_scope_generation=previous.channel_scope_generation)
            if thread.turn_generation != previous.turn_generation:
                # A stale metadata writer cannot reset a completed-turn fence.
                thread = replace(
                    thread,
                    turn_generation=previous.turn_generation,
                    last_finished_turn_id=(
                        previous.last_finished_turn_id
                        if thread.active_turn == previous.active_turn
                        else None
                    ),
                )
        elif any(existing.created_at == thread.created_at for existing in self.threads.values()):
            # The Windows wall clock can return the same value for six
            # independent default-constructed threads. Allocate a distinct
            # identity under this store lock, but never rewrite an explicit
            # caller-supplied creation identity or alias someone else's claim.
            if not thread._generated_created_at:
                raise RelationViolationError("Registry creation identities collide.")
            used = {existing.created_at for existing in self.threads.values()}
            candidate = float(thread.created_at)
            while candidate in used:
                candidate = math.nextafter(candidate, math.inf)
            if not math.isfinite(candidate):
                raise RelationViolationError("Registry creation identities collide.")
            thread = replace(thread, created_at=candidate)

        return RegistrationChange(previous, previous_status, thread, status, new_owner)

    def apply_registration(self, change: RegistrationChange) -> None:
        previous, previous_status = change.previous, change.previous_status
        thread, status, new_owner = change.thread, change.status, change.new_owner
        self.threads[thread.name] = thread
        self.statuses[thread.name] = status
        self.last_seen[thread.name] = time.time()
        if (
            previous is None
            or new_owner
            or previous.pid != thread.pid
            or previous.role != thread.role
            or (previous_status is not None and previous_status.changes_owner(status))
        ):
            self.admissions.advance(thread.name)
            self.owners.advance(thread.name)
        if thread.active_turn is not None and (
            previous is None or new_owner or thread.active_turn != previous.active_turn
        ):
            thread = replace(
                thread, active_turn=replace(thread.active_turn, admission_generation=None)
            )
            self.threads[thread.name] = thread

    def restore_stopped(self, source: RegistrySnapshot, names: Sequence[str]) -> tuple[str, ...]:
        """Restore selected missing identities without importing execution authority.

        Current declarations always win. An explicit identity collision refuses
        the whole selection before writing; callers can inspect and select the
        unambiguous records. Saved sessions and metadata retain their original
        provenance, but old PIDs, active turns and admission epochs do not travel.
        No bus, delivery cursor, pending input or coordinator row is copied.
        """
        selected = tuple(dict.fromkeys(names))
        additions: list[Thread] = []
        identities = {thread.created_at: name for name, thread in self.threads.items()}
        for name in selected:
            thread = source.threads[name]
            existing = self.threads.get(name)
            if existing is not None:
                if existing.created_at != thread.created_at:
                    raise RelationViolationError(f"Restoration identity conflicts for {name!r}")
                continue
            if name in self.aliases or thread.created_at in identities:
                raise RelationViolationError(f"Restoration identity conflicts for {name!r}")
            identities[thread.created_at] = name
            additions.append(replace(thread, pid=0, active_turn=None))
        restored = {thread.name: thread for thread in additions}
        available = self.threads | restored
        aliases = {
            alias: canonical
            for alias, canonical in source.aliases.items()
            if canonical in available
            and available[canonical].created_at == source.threads[canonical].created_at
            and alias not in available
            and alias not in self.aliases
        }
        for thread in additions:
            self.threads[thread.name] = thread
            self.statuses[thread.name] = source.statuses[thread.name].restored()
            self.last_seen[thread.name] = source.last_seen.get(thread.name, 0.0)
            self.admissions.advance(thread.name)
            self.owners.advance(thread.name)
        if additions or aliases:
            self.aliases.update(aliases)
        return tuple(restored)

    def rename(self, name: str, new_name: str) -> tuple[str, str]:
        """Rename one running thread while retaining old names as aliases."""
        canonical = self.aliases.get(name, name)
        if canonical not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        if new_name == canonical:
            return canonical, canonical
        current = self.threads[canonical]
        if not self.statuses[canonical].running:
            raise RelationViolationError("Only a running thread can rename itself.")
        alias_owner = self.aliases.get(new_name)
        if new_name in self.threads or (alias_owner is not None and alias_owner != canonical):
            raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
        replacement = replace(current, name=new_name)
        if alias_owner == canonical:
            del self.aliases[new_name]
        status = self.statuses.pop(canonical)
        last_seen = self.last_seen.pop(canonical)
        del self.threads[canonical]
        self.threads[new_name] = replacement
        self.statuses[new_name] = status
        self.last_seen[new_name] = last_seen
        for child_name, child in tuple(self.threads.items()):
            if child.parent == canonical:
                self.threads[child_name] = replace(child, parent=new_name)
        for alias, target in tuple(self.aliases.items()):
            if target == canonical:
                self.aliases[alias] = new_name
        self.aliases[canonical] = new_name
        self.admissions.rename(canonical, new_name)
        self.owners.rename(canonical, new_name)
        return canonical, new_name

    def fence_idle_owner(self, expected: Thread, *, expected_admission_generation: int) -> int:
        """Atomically deny new turns for exactly one idle owner before signaling.

        The outer wire lock alone cannot exclude a direct registry claim; this
        check and the STOPPED transition share the registry's own lock.
        """
        current = self.threads.get(expected.name)
        status = self.statuses.get(expected.name)
        if (
            current != expected
            or current is None
            or current.active_turn is not None
            or status is None
            or not status.active
            or self.admissions.generations.get(expected.name) != expected_admission_generation
        ):
            raise RelationViolationError("Idle owner changed before restart fence.")
        self.statuses[expected.name] = StoppedThreadStatus()
        self.admissions.advance(expected.name)
        self.owners.advance(expected.name)
        return self.admissions.generations[expected.name]

    def unregister(self, name: str) -> None:
        name = self.aliases.get(name, name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        if self.statuses[name].active:
            self.owners.advance(name)
        self.statuses[name] = StoppedThreadStatus()
        self.threads[name] = replace(self.threads[name], active_turn=None)
        self.admissions.advance(name)

    def archive(self, name: str) -> None:
        name = self.aliases.get(name, name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        if self.statuses[name].active:
            self.owners.advance(name)
        self.statuses[name] = ArchivedThreadStatus()
        self.admissions.advance(name)

    def begin_delete(self, name: str) -> None:
        name = self.aliases.get(name, name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        self.statuses[name] = self.statuses[name].for_deletion()
        self.admissions.advance(name)

    def remove(self, name: str) -> tuple[str, ...]:
        """Remove a declaration and atomically detach its surviving children."""
        name = self.aliases.get(name, name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        detached = tuple(
            sorted(child.name for child in self.threads.values() if child.parent == name)
        )
        for child_name in detached:
            self.threads[child_name] = replace(self.threads[child_name], parent=None)
        if self.statuses[name].active:
            self.owners.advance(name)
        del self.threads[name]
        self.statuses.pop(name, None)
        self.last_seen.pop(name, None)
        self.admissions.advance(name)
        self.aliases = {alias: target for alias, target in self.aliases.items() if target != name}
        return detached

    def heartbeat(self, name: str) -> None:
        name = self.aliases.get(name, name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        previous = self.statuses[name]
        resumed = previous.after_heartbeat(name)
        if previous.changes_owner(resumed):
            self.admissions.advance(name)
            self.owners.advance(name)
        self.statuses[name] = resumed
        self.last_seen[name] = time.time()

    def lease_turn(
        self, current: Thread, turn_id: str, routing: TurnRouting | None
    ) -> tuple[Thread, int]:
        """Caller holds the registry lock and has checked live turn ownership."""
        if current.turn_generation >= (1 << 63) - 1:
            raise RelationViolationError("Turn generation exhausted")
        leased = replace(
            current,
            turn_generation=current.turn_generation + 1,
            last_finished_turn_id=None,
            active_turn=ActiveTurn(
                turn_id,
                current.pid,
                routing=routing,
                admission_generation=self.admissions.generations[current.name],
                turn_generation=current.turn_generation + 1,
            ),
        )
        self.threads[current.name] = leased
        self.last_seen[current.name] = time.time()
        owner_generation = self.owners.generations[current.name]
        return leased, owner_generation

    def release_turn(self, lease: TurnLeaseFence) -> tuple[bool, FinishedTurnFence | None]:
        """Release only this exact lease; a revoked admission cannot attest completion."""
        name = self.aliases.get(lease.identity.incarnation.name, lease.identity.incarnation.name)
        current = self.threads.get(name)
        if (
            current is None
            or current.active_turn is None
            or current.active_turn.id != lease.turn_id
            or current.created_at != lease.identity.incarnation.created_at
            or current.turn_generation != lease.identity.generation
            or current.active_turn.turn_generation != lease.identity.generation
            or current.active_turn.admission_generation != lease.admission_generation
        ):
            return False, None
        admission = lease.admission_generation
        attested = (
            current.turn_generation > 0
            and admission > 0
            and self.admissions.generations.get(name) == admission
            and self.statuses[name].active
        )
        self.threads[name] = replace(
            current,
            active_turn=None,
            last_finished_turn_id=(lease.turn_id if current.turn_generation else None),
        )
        self.last_seen[name] = time.time()
        if not attested:
            return True, None
        assert current.turn_identity is not None
        return True, FinishedTurnFence(
            identity=current.turn_identity,
            turn_id=lease.turn_id,
            admission_generation=admission,
        )


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    threads: Mapping[str, Thread]
    statuses: Mapping[str, ThreadStatus]
    last_seen: Mapping[str, float]
    aliases: Mapping[str, str]
    owner_generations: Mapping[str, int]
    admission_generations: Mapping[str, int]

    def owner_identity(self, name: str) -> OwnerIdentity:
        canonical = self.aliases.get(name, name)
        return self.threads[canonical].owner_identity(self.owner_generations[canonical])
