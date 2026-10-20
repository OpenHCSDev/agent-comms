"""One registry document: thread facts, identity domains and lifecycle behavior."""

from __future__ import annotations

import math
import time
import typing
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from agent_comms import field_codec

from .child_process import NoProcess, ProcessIdentity
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import FieldCodec, MalformedRecord
from .registration_change import (
    InitialRegistration,
    RegistrationChange,
    UpdatedRegistration,
)
from .registry_provenance import (
    RegisteredRecipient,
    RegistryNames,
    RegistryProvenance,
    RetiredRecipient,
)
from .restart_refusals import (
    OwnerBusyRefusal,
    OwnerChangedBeforeFenceRefusal,
    OwnerGenerationChangedRefusal,
)
from .thread_identity import (
    ClaimedIdentity,
    GenerationCounter,
    OwnerIdentity,
    RecordedThreadIncarnation,
    TurnIdentity,
)
from .thread_presentation import ThreadOwnerBinding
from .thread_status import (
    ArchivedThreadStatus,
    StoppedThreadStatus,
    ThreadStatus,
)
from .threads import Thread
from .turn_lease import FinishedTurnFence, NoFinishedTurn, TurnCompletion, TurnLeaseFence

if TYPE_CHECKING:
    from .audience_manifest import FrozenRecipient
    from .bus_publication import CommittedDelivery


@dataclass(frozen=True, slots=True)
class RosterVisibility:
    """Which threads a roster shows: the one declaration of its defaults."""

    stopped: bool = True
    archived: bool = False


DEFAULT_ROSTER = RosterVisibility()


class RegistryPresence[StatusMap: Mapping[str, ThreadStatus]](RegistryNames[Thread]):
    """Presence reads shared by the acquired document and its detached cut."""

    __slots__ = ()

    statuses: StatusMap
    last_seen: Mapping[str, float]

    def set_status(self: RegistryPresence[dict[str, ThreadStatus]], name: str, status: ThreadStatus) -> None:
        self.statuses[name] = status.declaration()

    def status(self, name: str) -> ThreadStatus:
        thread = self.require(name)
        return self.statuses[thread.name].registered(thread)

    def require_active(self, name: str) -> Thread:
        try:
            thread = self.require(name)
            self.status(thread.name).require_active()
        except UnregisteredThreadError as error:
            raise RelationViolationError("live owner is stopped or unavailable") from error
        return thread

    def seen_at(self, name: str) -> float:
        return self.last_seen[self.require(name).name]

    def visible_threads(
        self, visibility: RosterVisibility, *, names: Iterable[str] | None = None,
    ) -> Iterator[Thread]:
        selected = (self.threads.values() if names is None else
                    {thread.name: thread for thread in map(self.require, names)}.values())
        return (executor for thread in selected
                if self.status(thread.name).in_view(visibility)
                for executor in thread.role.executor(thread))

    def channel_audience(self) -> Iterator[Thread]:
        """A channel reaches the default roster: stopped owners receive, archived do not."""
        return self.visible_threads(DEFAULT_ROSTER)


class UserThreadLookup[Value](ABC):
    """The registry's human selection, including a registry with no human yet."""

    @abstractmethod
    def map[Other](self, resolve: Callable[[Value], Other]) -> UserThreadLookup[Other]: ...

    @abstractmethod
    def existing(self) -> tuple[Value, ...]: ...

    @abstractmethod
    def or_create(self, create: Callable[[], Value]) -> Value: ...

    @abstractmethod
    def require(self, refusal: Exception) -> Value: ...


@dataclass(frozen=True, slots=True)
class FoundUserThread[Value](UserThreadLookup[Value]):
    value: Value

    def map[Other](self, resolve: Callable[[Value], Other]) -> UserThreadLookup[Other]:
        return FoundUserThread(resolve(self.value))

    def existing(self) -> tuple[Value, ...]:
        return (self.value,)

    def or_create(self, create: Callable[[], Value]) -> Value:
        return self.value

    def require(self, refusal: Exception) -> Value:
        return self.value


@dataclass(frozen=True, slots=True)
class NoUserThread[Value](UserThreadLookup[Value]):
    def map[Other](self, resolve: Callable[[Value], Other]) -> UserThreadLookup[Other]:
        return NoUserThread()

    def existing(self) -> tuple[Value, ...]:
        return ()

    def or_create(self, create: Callable[[], Value]) -> Value:
        return create()

    def require(self, refusal: Exception) -> Value:
        raise refusal


class ThreadSlice[Value](ABC):
    """How one thread's part of a registry document field is cut from the wire, and
    how the field is copied. Every document field names its kind in its metadata."""

    @staticmethod
    def canonical(raw: dict, name: str) -> str:
        return name

    @staticmethod
    @abstractmethod
    def cut(raw: dict, canonical: str) -> dict:
        """This field's wire value restricted to ``canonical``."""

    @classmethod
    @abstractmethod
    def copy(cls, value: Value) -> Value:
        """A copy an edit can change without changing ``value``."""

    @staticmethod
    def of(plan: field_codec.FieldPlan) -> type[ThreadSlice]:
        kind = plan.setting(field_codec.SlicePolicy, None)
        if kind is None or not issubclass(kind, ThreadSlice):
            raise TypeError(f"{plan.name} declares no thread slice")
        return kind

    @staticmethod
    def required[Document: type](document: Document) -> Document:
        """Refuse, at class definition, a document field without a slice kind."""
        for plan in FieldCodec.field_plans(document):
            if plan.declared.init and plan.records:
                ThreadSlice.of(plan)
        return document


class OwnRow(ThreadSlice[dict]):
    """The row under the thread's own name."""

    @staticmethod
    def cut(raw: dict, canonical: str) -> dict:
        return {canonical: raw[canonical]} if canonical in raw else {}

    @classmethod
    def copy(cls, value: dict) -> dict:
        return dict(value)


class AliasesOf(OwnRow):
    """The aliases that point at the thread; they also resolve its canonical name."""

    @staticmethod
    def canonical(raw: dict[str, str], name: str) -> str:
        return raw.get(name, name)

    @staticmethod
    def cut(raw: dict, canonical: str) -> dict:
        return {alias: target for alias, target in raw.items() if target == canonical}


class GenerationOf(ThreadSlice[GenerationCounter]):
    """A generation counter whose generations are restricted to the thread."""

    @staticmethod
    def cut(raw: dict, canonical: str) -> dict:
        return {**raw, "generations": OwnRow.cut(raw["generations"], canonical)}

    @classmethod
    def copy(cls, value: GenerationCounter) -> GenerationCounter:
        return GenerationCounter(value.counter, dict(value.generations))


class WholeDocumentValue(ThreadSlice[int]):
    """A scalar shared by the whole document and every one-thread view."""

    @staticmethod
    def cut(raw: int, canonical: str) -> int:
        return raw

    @classmethod
    def copy(cls, value: int) -> int:
        return value


@ThreadSlice.required
@dataclass(slots=True)
class RegistryDocument(RegistryPresence[dict[str, ThreadStatus]]):
    version: typing.Annotated[
        Literal[1], field_codec.SlicePolicy(WholeDocumentValue), field_codec.WireRequired(True)
    ] = field(default=1)
    threads: typing.Annotated[dict[str, Thread], field_codec.SlicePolicy(OwnRow)] = field(
        default_factory=dict
    )
    statuses: typing.Annotated[dict[str, ThreadStatus], field_codec.SlicePolicy(OwnRow)] = field(
        default_factory=dict
    )
    last_seen: typing.Annotated[dict[str, float], field_codec.SlicePolicy(OwnRow)] = field(
        default_factory=dict
    )
    aliases: typing.Annotated[dict[str, str], field_codec.SlicePolicy(AliasesOf)] = field(
        default_factory=dict
    )
    owners: typing.Annotated[GenerationCounter, field_codec.SlicePolicy(GenerationOf)] = field(
        default_factory=GenerationCounter
    )
    claims: typing.Annotated[
        GenerationCounter, field_codec.WireName("admissions"), field_codec.SlicePolicy(GenerationOf)
    ] = field(default_factory=GenerationCounter)

    _human: UserThreadLookup[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # This answer belongs to the decoded revision. Keep the name, not an
        # unbound Thread: store binding and later reads use the current row.
        self._human = next((FoundUserThread(human.name)
                            for thread in self.threads.values()
                            for human in thread.role.human(thread)), NoUserThread())

    def user_thread(self) -> UserThreadLookup[Thread]:
        return self._human.map(self.require)

    def copy(self) -> RegistryDocument:
        return replace(
            self,
            **{
                plan.name: ThreadSlice.of(plan).copy(getattr(self, plan.name))
                for plan in FieldCodec.field_plans(type(self))
                if plan.declared.init and plan.records
            },
        )

    def install_thread(self, thread: Thread) -> None:
        """Retire a withdrawn reference inside this registry transaction."""
        if (
            thread.name in self.threads
            and self.threads[thread.name].turn_record != thread.turn_record
        ):
            previous = self.threads[thread.name]
            previous.turn_record.without_claim()
        self.threads[thread.name] = thread

    def advance_claim(self, name: str) -> int:
        """Remove the thread's permission to publish and advance its input claim."""
        thread = self.threads.get(name)
        if thread is not None:
            thread.turn_record.without_claim()
        return self.claims.advance(name)

    def entry(self, name: str) -> RegistryEntry:
        thread = self.require(name)
        return RegistryEntry(
            thread=thread,
            status=self.status(thread.name),
            last_seen=self.last_seen[thread.name],
            owner_generation=self.owners.generations[thread.name],
            claimed_generation=self.claims.generations[thread.name],
            names=self.names_for(thread.name),
        )

    def entry_if_present(self, name: str) -> RegistryEntry | None:
        return self.entry(name) if self.canonical_name(name) in self.threads else None

    @classmethod
    def entry_from_wire(cls, raw: dict, name: str) -> RegistryEntry:
        """Decode one thread's entry without decoding the other declarations.

        Only this thread's rows go through the document decoder; another
        thread's declaration is neither decoded nor validated here.
        """
        try:
            keyed = [
                (ThreadSlice.of(plan), plan.key)
                for plan in FieldCodec.field_plans(cls)
                if plan.declared.init and plan.records
            ]
            canonical = name
            for kind, key in keyed:
                canonical = kind.canonical(raw[key], canonical)
            restricted = {key: kind.cut(raw[key], canonical) for kind, key in keyed}
        except (AttributeError, KeyError, TypeError) as error:
            raise RelationViolationError(f"Invalid registry document: {error!r}") from error
        return cls.from_wire(restricted).entry(name)

    @classmethod
    def thread_rows(cls) -> tuple[str, ...]:
        """The sections that hold one row per thread name."""
        return tuple(
            plan.name
            for plan in FieldCodec.field_plans(cls)
            if plan.declared.init and plan.records and ThreadSlice.of(plan) is OwnRow
        )

    @classmethod
    def from_wire(cls, raw: dict, earlier: tuple[dict, RegistryDocument] | None = None
                  ) -> RegistryDocument:
        """Decode a registry revision.

        ``earlier`` is another revision's raw rows with the document decoded
        from them. A thread row equal to its earlier raw row keeps the earlier
        decoded value (declarations and statuses are immutable), so a revision
        that changed one thread decodes one thread. Every row still comes from
        the one codec, and the whole document is checked as before.
        """
        try:
            if earlier is None:
                document = FieldCodec.decode(cls, raw)
            else:
                earlier_raw, decoded = earlier
                thread_rows = cls.thread_rows()
                if not isinstance(raw, dict) or any(
                    not isinstance(raw.get(section), dict) for section in thread_rows
                ):
                    raise MalformedRecord("registry thread sections must be objects")
                kept = {
                    section: {
                        name for name, row in raw[section].items()
                        if name in getattr(decoded, section)
                        and name in earlier_raw[section] and earlier_raw[section][name] == row
                    }
                    for section in thread_rows
                }
                changed = FieldCodec.decode(cls, {**raw, **{
                    section: {name: row for name, row in raw[section].items()
                              if name not in kept[section]}
                    for section in thread_rows
                }})
                reused: dict[str, Any] = {
                    section: {
                        name: (getattr(decoded, section) if name in kept[section]
                               else getattr(changed, section))[name]
                        for name in raw[section]
                    }
                    for section in thread_rows
                }
                document = replace(changed, **reused)
            names = set(document.threads)
            if names != set(document.statuses) or names != set(document.last_seen):
                raise MalformedRecord("thread presence does not match declarations")
            if not names <= document.owners.generations.keys():
                raise MalformedRecord("missing owner generation")
            if not names <= document.claims.generations.keys():
                raise MalformedRecord("missing claim_scope generation")
            if any(name != thread.name for name, thread in document.threads.items()):
                raise MalformedRecord("thread name differs from its declaration")
            if any(
                target not in names or alias in names for alias, target in document.aliases.items()
            ):
                raise MalformedRecord("alias has no exclusive retained owner")
            return document
        except MalformedRecord as error:
            raise RelationViolationError(f"Invalid registry document: {error}") from error

    def prepare_declaration(self, thread: Thread, status: ThreadStatus) -> RegistrationChange:
        canonical = self.canonical_name(thread.name)
        requested = thread.for_registration(canonical, self.threads.get(canonical))
        change = self.prepare_registration(requested, status, new_owner=False)
        return change.declared(requested)

    def prepare_registration(
        self, thread: Thread, status: ThreadStatus, *, new_owner: bool
    ) -> RegistrationChange:
        status = thread.execution.registered_status(thread, status)
        if thread.name in self.aliases:
            raise RelationViolationError(
                f"Thread name {thread.name!r} is a permanent alias and cannot be reused."
            )
        if thread.name in self.statuses:
            self.status(thread.name).require_mutable(thread.name)
        previous = self.threads.get(thread.name)
        if previous is not None:
            thread = thread.preserve_registration_history(previous)
            change = UpdatedRegistration(
                thread=thread,
                status=status,
                previous=previous,
                previous_status=self.status(thread.name),
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
        provenance, but old PIDs, active turns and input generations do not travel.
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
            additions.append(replace(thread.without_turn(), process_identity=NoProcess()))
        restored = {thread.name: thread for thread in additions}
        available = self.threads | restored
        aliases = source.restorable_aliases(available, self.aliases)
        for thread in additions:
            self.threads[thread.name] = thread
            self.set_status(thread.name, source.status(thread.name).restored())
            self.last_seen[thread.name] = source.seen_at(thread.name)
            self.advance_claim(thread.name)
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
        self.status(canonical).require_self_rename()
        if self.taken(new_name, owner=canonical):
            raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
        replacement = replace(current, name=new_name)
        if new_name in self.aliases:  # free for this thread: one of its own aliases
            del self.aliases[new_name]
        status = self.statuses.pop(canonical)
        last_seen = self.last_seen.pop(canonical)
        del self.threads[canonical]
        self.threads[new_name] = replacement
        self.set_status(new_name, status)
        self.last_seen[new_name] = last_seen
        for child_name, child in tuple(self.threads.items()):
            if child.parent == canonical:
                self.threads[child_name] = replace(child, parent=new_name)
        for alias, target in tuple(self.aliases.items()):
            if target == canonical:
                self.aliases[alias] = new_name
        self.aliases[canonical] = new_name
        self.claims.rename(canonical, new_name)
        self.owners.rename(canonical, new_name)
        return canonical, new_name

    def fence_idle_owner(self, expected: Thread, *, expected_claimed_generation: int) -> int:
        """Atomically deny new turns for exactly one idle owner before signaling.

        The outer wire lock alone cannot exclude a direct registry claim; this
        check and the STOPPED transition share the registry's own lock.
        """
        current = self.require_active(expected.name)
        try:
            current.require_idle()
        except RelationViolationError as error:
            raise OwnerBusyRefusal() from error
        if current != expected:
            raise OwnerChangedBeforeFenceRefusal()
        if self.claims.generations[current.name] != expected_claimed_generation:
            raise OwnerGenerationChangedRefusal()
        self.set_status(expected.name, expected.execution.registered_status(
            expected, StoppedThreadStatus()))
        self.advance_claim(expected.name)
        self.owners.advance(expected.name)
        return self.claims.generations[expected.name]

    def unregister(self, name: str) -> None:
        name = self.require(name).name
        if self.status(name).active:
            self.owners.advance(name)
        thread = self.threads[name]
        self.set_status(name, thread.execution.registered_status(thread, StoppedThreadStatus()))
        self.advance_claim(name)
        self.threads[name] = thread.without_turn()

    def archive(self, name: str) -> None:
        name = self.require(name).name
        if self.status(name).active:
            self.owners.advance(name)
        self.set_status(name, ArchivedThreadStatus())
        self.advance_claim(name)

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

    def begin_delete_originals(self, originals: Sequence[Thread]) -> None:
        """Withdraw input permission first; the owner requires a stopped status."""
        self.require_originals(originals)
        for original in originals:
            self.begin_delete(original.name)

    def remove_originals(self, originals: Sequence[Thread]) -> dict[str, tuple[str, ...]]:
        """Remove deleting declarations, detaching children; generations stay as the record."""
        self.require_originals(originals)
        return {original.name: self.remove(original.name) for original in originals}

    def begin_delete(self, name: str) -> None:
        name = self.require(name).name
        self.set_status(name, self.status(name).for_deletion())
        self.advance_claim(name)

    def remove(self, name: str) -> tuple[str, ...]:
        """Remove a declaration and atomically detach its surviving children."""
        name = self.require(name).name
        detached = tuple(
            sorted(child.name for child in self.threads.values() if child.parent == name)
        )
        for child_name in detached:
            self.threads[child_name] = replace(self.threads[child_name], parent=None)
        if self.status(name).active:
            self.owners.advance(name)
        self.advance_claim(name)
        del self.threads[name]
        self.statuses.pop(name, None)
        self.last_seen.pop(name, None)
        self.aliases = {alias: target for alias, target in self.aliases.items() if target != name}
        return detached

    def heartbeat(self, name: str) -> None:
        name = self.require(name).name
        previous = self.status(name)
        resumed = previous.after_heartbeat(name)
        if previous.changes_owner(resumed):
            self.advance_claim(name)
            self.owners.advance(name)
        self.set_status(name, resumed)
        self.last_seen[name] = time.time()

    def release_turn(self, lease: TurnLeaseFence) -> tuple[bool, TurnCompletion]:
        """Release only this exact lease; a revoked input grant cannot confirm completion."""
        name = self.canonical_name(lease.identity.incarnation.name)
        current = self.threads.get(name)
        if current is None:
            return False, NoFinishedTurn()
        canonical_lease = lease.renamed(name)
        record = current.turn_record
        if record != canonical_lease.record:
            return False, NoFinishedTurn()
        if TurnIdentity(current.incarnation, current.turn_generation) != canonical_lease.identity:
            return False, NoFinishedTurn()
        turn = record.read().require_active()
        if not turn.may_release(canonical_lease):
            return False, NoFinishedTurn()
        attested = (canonical_lease.can_attest(self.claims.generations[name])
                    and turn.held_by(canonical_lease) and self.status(name).active)
        keeps_resume = turn.phase.keeps_project_resume
        record.without_claim()
        self.threads[name] = replace(
            current.without_turn(),
            last_finished_turn_id=(lease.turn_id if current.turn_generation else None),
            project_resume_due=current.project_resume_due and keeps_resume,
        )
        self.last_seen[name] = time.time()
        if not attested:
            return True, NoFinishedTurn()
        return True, FinishedTurnFence(
            identity=canonical_lease.identity,
            turn_id=lease.turn_id,
            claimed_generation=lease.claimed_generation,
        )


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """Everything the registry records about one thread, and nothing about the others.

    The document-wide generation counters are allocation state, not this
    thread's entry; only the generations assigned to this thread belong here.
    """

    thread: Thread
    status: ThreadStatus
    last_seen: float
    owner_generation: int
    claimed_generation: int
    names: frozenset[str]

    def matches_name(self, name: str) -> bool:
        return name in self.names

    def owned_by(self, owner: OwnerIdentity, process: ProcessIdentity) -> bool:
        return (
            self.status.active
            and self.matches_name(owner.incarnation.name)
            and owner.owns(self.thread, self.owner_generation, process)
        )

    def require_owner_process(self, owner: OwnerIdentity, process: ProcessIdentity) -> Thread:
        """The exact saved owner lease, current name and process still agree."""
        if not self.matches_name(owner.incarnation.name):
            raise RelationViolationError("Thread incarnation changed during owner attachment")
        self.status.require_active()
        owner.require_current(self.thread, self.owner_generation, process)
        return self.thread

    @property
    def owner_identity(self) -> OwnerIdentity:
        return OwnerIdentity(self.thread.incarnation, self.owner_generation)


@dataclass(frozen=True, slots=True)
class RegistrySnapshot(RegistryPresence[Mapping[str, ThreadStatus]], RegistryProvenance):
    threads: Mapping[str, Thread]
    statuses: Mapping[str, ThreadStatus]
    last_seen: Mapping[str, float]
    aliases: Mapping[str, str]
    owner_generations: Mapping[str, int]
    claimed_generations: Mapping[str, int]
    by_lookup: Mapping[str, Thread] = field(repr=False, compare=False)

    def threads_for(self, incarnation: RecordedThreadIncarnation) -> tuple[Thread, ...]:
        return tuple(self.by_lookup[key] for key in (incarnation.lookup,) if key in self.by_lookup)

    def idle_after(self, fence: FinishedTurnFence) -> tuple[Thread, ...]:
        return tuple(
            thread
            for thread in self.threads_for(fence.identity.incarnation.require_recorded())
            if self.status(thread.name).active
            and thread.latest_idle_turn(fence, self.claimed_generations.get(thread.name, 0))
        )

    @classmethod
    def capture(cls, document: RegistryDocument) -> RegistrySnapshot:
        """One live registry cut owns the index and all its source maps."""
        indexed: dict[str, Thread] = {}
        for thread in document.threads.values():
            lookup = thread.incarnation.require_recorded().lookup
            if lookup in indexed:
                raise RelationViolationError("Registry creation identities collide.")
            indexed[lookup] = thread
        return cls(
            threads=MappingProxyType(dict(document.threads)),
            statuses=MappingProxyType(dict(document.statuses)),
            last_seen=MappingProxyType(dict(document.last_seen)),
            aliases=MappingProxyType(dict(document.aliases)),
            owner_generations=MappingProxyType(dict(document.owners.generations)),
            claimed_generations=MappingProxyType(dict(document.claims.generations)),
            by_lookup=MappingProxyType(indexed),
        )

    def displayed_recipient_effect(self, reads, message, thread: Thread, *, document=None):
        return thread.role.displayed_recipient_effect(reads, message, thread, self, document)

    def recipient(
        self, recipient: FrozenRecipient, original: CommittedDelivery
    ) -> RegisteredRecipient | RetiredRecipient:
        """A missing live thread was retired only if the original send names it."""
        lookup = recipient.recipient_lookup
        if lookup in self.by_lookup:
            thread = self.by_lookup[lookup]
            if thread.name != self.canonical_name(recipient.canonical_thread):
                raise RelationViolationError("Frozen recipient name does not match its lookup.")
            return RegisteredRecipient(thread)
        if recipient not in original.audience.recipients:
            raise UnregisteredThreadError("Recipient lookup is not in the original message.")
        return RetiredRecipient(lookup)

    def retired(self, name: str) -> bool:
        """Removal keeps the name's generations; they are the deletion record."""
        return name not in self.threads and name in self.claimed_generations

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

    def owner_identity(self, name: str) -> OwnerIdentity:
        canonical = self.canonical_name(name)
        return OwnerIdentity(self.threads[canonical].incarnation, self.owner_generations[canonical])

    def claimed_identity(self, name: str) -> ClaimedIdentity:
        canonical = self.canonical_name(name)
        return ClaimedIdentity(self.threads[canonical].incarnation, self.claimed_generations[canonical])

    def owner_binding(self, name: str) -> ThreadOwnerBinding:
        canonical = self.canonical_name(name)
        thread = self.threads[canonical]
        return thread.execution.owner_binding(self, thread)

    def require_owner_process(self, owner: OwnerIdentity, process: ProcessIdentity) -> None:
        """A read attachment retains its owner lease across startup, not across restart."""
        thread = self.require_active(owner.incarnation.name)
        owner.require_current(thread, self.owner_generations[thread.name], process)

    def require_stopping_owner(self, expected: Thread, claim_scope: int) -> None:
        """A fenced stop may target a stopped owner, but never a later birth/lease."""
        try:
            current = self.threads[expected.name]
        except KeyError as error:
            raise RelationViolationError("Stopping owner was removed") from error
        if current.incarnation != expected.incarnation:
            raise RelationViolationError("Stopping owner incarnation changed")
        expected.role.require_executable(RelationViolationError("Stopping owner was not an agent"))
        current.role.require_executable(RelationViolationError("Stopping owner is not an agent"))
        if current.process_identity != expected.process_identity:
            raise RelationViolationError("Stopping owner process changed")
        if self.claimed_generations[expected.name] != claim_scope:
            raise RelationViolationError("Stopping owner claim_scope changed")
