"""One registry document: thread facts, identity domains and lifecycle behavior."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .child_process import ProcessIdentity
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import FieldCodec
from .registration_change import InitialRegistration, NativeSourcePublication, RegistrationChange, UpdatedRegistration
from .routing import TurnRouting
from .restart_refusals import (
    OwnerBusyRefusal,
    OwnerChangedBeforeFenceRefusal,
    OwnerGenerationChangedRefusal,
)
from .thread_identity import AdmissionIdentity, GenerationCounter, OwnerIdentity
from .thread_presentation import ThreadOwnerBinding
from .thread_status import (
    ArchivedThreadStatus,
    StoppedThreadStatus,
    ThreadStatus,
)
from .threads import Thread
from .registry_provenance import RegistryNames, RegistryProvenance
from .turn_lease import ActiveTurn, FinishedTurnFence, TurnLeaseFence, TurnState
from .turn_phase import TurnPhase

if TYPE_CHECKING:
    from .native_input_owner import RegistryOwner


class RegistryPresence(RegistryNames[Thread]):
    """Presence reads shared by the acquired document and its detached cut."""

    __slots__ = ()

    statuses: Mapping[str, ThreadStatus]
    last_seen: Mapping[str, float]

    def status(self, name: str) -> ThreadStatus:
        canonical = self.canonical_name(name)
        try:
            return self.statuses[canonical]
        except KeyError as error:
            raise UnregisteredThreadError(f"Thread {canonical!r} is not registered.") from error

    def require_active(self, name: str) -> Thread:
        try:
            thread = self.require(name)
            self.status(thread.name).require_active()
        except UnregisteredThreadError as error:
            raise RelationViolationError("live owner is stopped or unavailable") from error
        return thread

    def seen_at(self, name: str) -> float:
        return self.last_seen[self.require(name).name]


@dataclass(slots=True)
class RegistryDocument(RegistryPresence):
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
            threads=dict(self.threads),
            statuses=dict(self.statuses),
            last_seen=dict(self.last_seen),
            aliases=dict(self.aliases),
            owner_generations=dict(self.owners.generations),
            admission_generations=dict(self.admissions.generations),
        )

    @classmethod
    def from_wire(cls, raw: dict) -> RegistryDocument:
        try:
            document = FieldCodec.decode(cls, raw)
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

    def claim_name(self, base_name: str) -> str:
        """Allocate from current declarations and permanent aliases under the registry lock."""
        name = base_name
        suffix = 2
        while name in self.threads or name in self.aliases:
            name = f"{base_name}-{suffix}"
            suffix += 1
        return name

    def prepare_declaration(self, thread: Thread, status: ThreadStatus) -> RegistrationChange:
        canonical = self.canonical_name(thread.name)
        requested = thread.for_registration(canonical, self.threads.get(canonical))
        change = self.prepare_registration(requested, status, new_owner=False)
        return change.declared(requested)

    def prepare_registration(
        self, thread: Thread, status: ThreadStatus, *, new_owner: bool
    ) -> RegistrationChange:
        if thread.name in self.aliases:
            raise RelationViolationError(
                f"Thread name {thread.name!r} is a permanent alias and cannot be reused."
            )
        if (current := self.statuses.get(thread.name)) is not None:
            current.require_mutable(thread.name)
        previous = self.threads.get(thread.name)
        if previous is not None:
            thread = thread.preserve_registration_history(previous)
            change = UpdatedRegistration(
                thread=thread,
                status=status,
                previous=previous,
                previous_status=self.statuses[thread.name],
            )
            return change.restarted() if new_owner else change
        if any(existing.created_at == thread.created_at for existing in self.threads.values()):
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

        change = InitialRegistration(thread=thread, status=status)
        return change.restarted() if new_owner else change

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
            additions.append(replace(thread, process_identity=None, active_turn=None))
        restored = {thread.name: thread for thread in additions}
        available = self.threads | restored
        aliases = source.restorable_aliases(available, self.aliases)
        for thread in additions:
            self.threads[thread.name] = thread
            self.statuses[thread.name] = source.statuses[thread.name].restored()
            self.last_seen[thread.name] = source.seen_at(thread.name)
            self.admissions.advance(thread.name)
            self.owners.advance(thread.name)
        if additions or aliases:
            self.aliases.update(aliases)
        return tuple(restored)

    def rename(self, name: str, new_name: str) -> tuple[str, str]:
        """Rename one running thread while retaining old names as aliases."""
        canonical = self.canonical_name(name)
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
        snapshot = self.snapshot()
        current = snapshot.require_active(expected.name)
        try:
            current.require_idle()
        except RelationViolationError as error:
            raise OwnerBusyRefusal() from error
        if current != expected:
            raise OwnerChangedBeforeFenceRefusal()
        if snapshot.admission_generations[current.name] != expected_admission_generation:
            raise OwnerGenerationChangedRefusal()
        self.statuses[expected.name] = StoppedThreadStatus()
        self.admissions.advance(expected.name)
        self.owners.advance(expected.name)
        return self.admissions.generations[expected.name]

    def unregister(self, name: str) -> None:
        name = self.canonical_name(name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        if self.statuses[name].active:
            self.owners.advance(name)
        self.statuses[name] = StoppedThreadStatus()
        self.threads[name] = replace(self.threads[name], active_turn=None)
        self.admissions.advance(name)

    def archive(self, name: str) -> None:
        name = self.canonical_name(name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        if self.statuses[name].active:
            self.owners.advance(name)
        self.statuses[name] = ArchivedThreadStatus()
        self.admissions.advance(name)

    def require_originals(self, originals: Sequence[Thread]) -> None:
        for original in originals:
            if not original.incarnation.current(self):
                raise RelationViolationError("Tagged thread incarnation changed before removal.")

    def archive_originals(self, originals: Sequence[Thread]) -> None:
        self.require_originals(originals)
        for original in originals:
            self.status(original.name).require_stopped()
            self.archive(original.name)

    def change_tag(self, originals: Sequence[Thread], tag: str, replacement: str | None) -> None:
        """Change one cohort's tags on current records, retaining owner state."""
        self.require_originals(originals)
        for original in originals:
            current = self.require(original.name)
            tags = (current.tags - {tag}) | ({replacement} if replacement else set())
            change = self.prepare_registration(
                replace(current, tags=tags), self.status(current.name), new_owner=False)
            change.apply(self)

    def delete_originals(self, originals: Sequence[Thread]) -> None:
        self.require_originals(originals)
        for original in originals:
            self.begin_delete(original.name)
        for original in originals:
            self.remove(original.name)

    def begin_delete(self, name: str) -> None:
        name = self.canonical_name(name)
        if name not in self.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        self.statuses[name] = self.statuses[name].for_deletion()
        self.admissions.advance(name)

    def remove(self, name: str) -> tuple[str, ...]:
        """Remove a declaration and atomically detach its surviving children."""
        name = self.canonical_name(name)
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
        name = self.canonical_name(name)
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
        name = self.canonical_name(lease.identity.incarnation.name)
        current = self.threads.get(name)
        if current is None:
            return False, None
        canonical_lease = lease.renamed(name)
        if current.turn_lease != canonical_lease:
            return False, None
        attested = canonical_lease.can_attest(self.admissions.generations[name])
        attested = attested and self.statuses[name].active
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
            admission_generation=lease.admission_generation,
        )

    def _turn_effects(
        self, lease: TurnLeaseFence, observe: Callable[[TurnState], Iterable[TurnState]],
    ) -> tuple[TurnState, ...]:
        """Capture publication effects from the exact fenced document mutation."""
        name = self.canonical_name(lease.identity.incarnation.name)
        current = self.threads.get(name)
        if current is None or current.turn_lease != lease.renamed(name):
            return ()
        effects = tuple(observe(current.turn_state))
        for state in effects:
            self.threads[name] = replace(current, active_turn=state.active)
        return effects

    def transition_turn(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        return self._turn_effects(lease, lambda state: state.phase_effects(phase))

    def observe_native_phase(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        return self._turn_effects(lease, lambda state: state.native_phase_effects(phase))

    def prepare_native_source(self, original: RegistryOwner, session_file: str) -> NativeSourcePublication:
        """Publish only the source fact of an already admitted original owner."""
        current = original.require_source_snapshot(self.snapshot())
        return self._native_source_publication(current, session_file)

    def prepare_idle_native_source(self, original: RegistryOwner, session_file: str) -> NativeSourcePublication:
        """Retain the idle owner's current turn/task records during selection."""
        current = original.require_idle_source_snapshot(self.snapshot())
        return self._native_source_publication(current, session_file)

    def _native_source_publication(self, current: Thread, session_file: str) -> NativeSourcePublication:
        return NativeSourcePublication(
            thread=replace(current, session_file=session_file),
            status=self.statuses[current.name],
            previous=current, previous_status=self.statuses[current.name],
        )


@dataclass(frozen=True, slots=True)
class RegistrySnapshot(RegistryPresence, RegistryProvenance):
    threads: Mapping[str, Thread]
    statuses: Mapping[str, ThreadStatus]
    last_seen: Mapping[str, float]
    aliases: Mapping[str, str]
    owner_generations: Mapping[str, int]
    admission_generations: Mapping[str, int]

    def restorable_aliases(
        self, available: Mapping[str, Thread], retained: Mapping[str, str]
    ) -> dict[str, str]:
        matching = {
            name
            for name in self.threads.keys() & available.keys()
            if self.threads[name].incarnation == available[name].incarnation
        }
        unoccupied = self.aliases.keys() - available.keys() - retained.keys()
        return {
            alias: self.aliases[alias] for alias in unoccupied if self.aliases[alias] in matching
        }

    def require_unambiguous_ownership(self) -> None:
        """Archived identities remain readable; publication requires unique owners."""
        if len({thread.created_at for thread in self.threads.values()}) != len(self.threads):
            raise RelationViolationError("Registry creation identities collide.")

    def owner_identity(self, name: str) -> OwnerIdentity:
        canonical = self.canonical_name(name)
        return OwnerIdentity(self.threads[canonical].incarnation, self.owner_generations[canonical])

    def admission_identity(self, name: str) -> AdmissionIdentity:
        canonical = self.canonical_name(name)
        return AdmissionIdentity(self.threads[canonical].incarnation, self.admission_generations[canonical])

    def owner_binding(self, name: str) -> ThreadOwnerBinding:
        canonical = self.canonical_name(name)
        thread = self.threads[canonical]
        return thread.execution.owner_binding(self, thread)

    def require_owner_process(self, owner: OwnerIdentity, process: ProcessIdentity) -> None:
        """A read attachment retains its owner lease across startup, not across restart."""
        if not owner.incarnation.current(self):
            raise RelationViolationError("Thread incarnation changed during owner attachment")
        thread = self.require_active(owner.incarnation.name)
        if self.owner_generations[thread.name] != owner.generation:
            raise RelationViolationError("Owner lease changed during attachment")
        thread.require_local_process(process)

    def require_stopping_owner(self, expected: Thread, admission: int) -> None:
        """A fenced stop may target a stopped owner, but never a later birth/lease."""
        try:
            current = self.threads[expected.name]
        except KeyError as error:
            raise RelationViolationError("Stopping owner was removed") from error
        if current.incarnation != expected.incarnation:
            raise RelationViolationError("Stopping owner incarnation changed")
        current.require_local_process(expected.require_process())
        if self.admission_generations[expected.name] != admission:
            raise RelationViolationError("Stopping owner admission changed")
