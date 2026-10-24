"""One registry document: thread facts, identity domains and lifecycle behavior."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import ChainMap
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Annotated, Self

from pydantic import JsonValue


from .child_process import NoProcess, ProcessIdentity
from .declared_family import DeclaredFamily
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import (
    ExternalJsonShape,
    FieldCodec,
    MalformedRecord,
    FieldPlan,
    SlicePolicy,
    WireOmitDefault,
    WireRequired,
    WireValue,
)
from .registration_change import (
    InitialRegistration,
    RegistrationChange,
    UpdatedRegistration,
)
from .repo import Repo, RepositorySelection
from .repository_name import RepositoryName
from .registry_provenance import (
    DeletedThread,
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
    OfferedTools,
    OwnerIdentity,
    RecordedThreadIncarnation,
    ThreadIncarnation,
    ThreadAuthority,
    TurnIdentity,
)
from .thread_presentation import ThreadOwnerBinding
from .thread_status import (
    ActiveThreadPresence,
    ArchivedThreadStatus,
    StoppedThreadStatus,
    ThreadStatus,
)
from .thread_text import ForkedParent, NoParent
from .threads import Thread, ThreadWorkState
from .turn_lease import (
    FinishedTurn, FinishedTurnFence, NoFinishedTurn, StoredTurnRecord, TurnCompletion, TurnLeaseFence,
    TurnRecord,
)

if TYPE_CHECKING:
    from .activity import ObservedActivity
    from .agent_activity import AgentActivity
    from .audience_manifest import FrozenRecipient
    from .bus_publication import Delivered


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

    def active_candidates(self) -> Iterator[Thread]:
        return (self.threads[name] for name, status in self.statuses.items()
                if status.may_be_active)

    def set_status(self: RegistryPresence[dict[str, ThreadStatus]], name: str, status: ThreadStatus) -> None:
        self.statuses[name] = status.declaration()

    def status(self, name: str) -> ThreadStatus:
        thread = self.require(name)
        return self.statuses[thread.name].registered(thread, self.last_seen[thread.name])

    def receiving(self, name: str) -> bool:
        """Whether a message sent now is read now; an unregistered name reads nothing."""
        return name in self.threads and self.status(name).receiving

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
        selected = ((self.threads[name] for name, status in self.statuses.items()
                     if status.in_view(visibility)) if names is None else
                    (self.threads[name] for name in dict.fromkeys(map(self.canonical_name, names))
                     if name in self.threads))
        return (executor for thread in selected
                if self.status(thread.name).in_view(visibility)
                for executor in thread.role.executor(thread))

    def active_threads(self, candidates: Iterable[Thread]) -> Iterator[Thread]:
        """Select activity-bearing presences from this already acquired registry cut."""
        return (member for thread in candidates
                for member in self.status(thread.name).items_with(ActiveThreadPresence, thread))

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
    def of(plan: FieldPlan) -> type[ThreadSlice]:
        for policy in SlicePolicy.single(plan.annotation):
            if issubclass(policy.value, ThreadSlice):
                return policy.value
        raise TypeError(f"{plan.name} declares no thread slice")

    @classmethod
    def required[Document: type](cls, document: Document) -> Document:
        """Refuse, at class definition, a document field without a slice kind."""
        for plan in FieldCodec.field_plans(document):
            if plan.declared.init and plan.records:
                cls.of(plan)
        return document


class SnapshotValue[Value](ThreadSlice[Value]):
    """A declared registry field owns both editable and snapshot copies."""

    @classmethod
    @abstractmethod
    def freeze(cls, value: Value) -> object: ...

    @staticmethod
    def of(plan: FieldPlan) -> type[SnapshotValue]:
        policy = ThreadSlice.of(plan)
        if not issubclass(policy, SnapshotValue):
            raise TypeError(f"{plan.name} declares no snapshot value policy")
        return policy


class OwnRow[Rows](SnapshotValue[Rows]):
    """The row under the thread's own name."""

    @staticmethod
    def cut(raw: dict, canonical: str) -> dict:
        return {canonical: raw[canonical]} if canonical in raw else {}

    @classmethod
    @abstractmethod
    def reuse(cls, rows: Mapping[str, object], kept: frozenset[str],
              earlier: Rows, changed: Rows) -> None: ...


class MappingRowsSlice(OwnRow[MutableMapping]):
    @classmethod
    def copy(cls, value: Mapping) -> dict:
        return dict(value)

    @classmethod
    def freeze(cls, value: Mapping) -> Mapping:
        return MappingProxyType(cls.copy(value))

    @classmethod
    def reuse(cls, rows: Mapping[str, object], kept: frozenset[str],
              earlier: MutableMapping, changed: MutableMapping) -> None:
        merged = {name: (earlier if name in kept else changed)[name] for name in rows}
        changed.clear()
        changed.update(merged)


class InvalidRegistryDocument(RelationViolationError):
    def __init__(self, error: Exception) -> None:
        super().__init__(f"Invalid registry document: {error}")


class ThreadRows(ExternalJsonShape, WireValue, MutableMapping[str, Thread]):
    """A revision's thread rows, decoded and bound only when read."""

    def __init__(self) -> None:
        self._raw: dict[str, JsonValue] = {}
        self._decoded: dict[str, Thread] = {}
        self._binding: Callable[[Thread], Thread] = self.unbound

    @staticmethod
    def unbound(thread: Thread) -> Thread:
        return thread

    def bind(self, binding: Callable[[Thread], Thread]) -> None:
        self._binding = binding
        self._decoded = {name: binding(thread) for name, thread in self._decoded.items()}

    def __getitem__(self, name: str) -> Thread:
        if name not in self._decoded:
            self._decoded[name] = self.decode_row(self._raw[name])
        return self._decoded[name]

    def decode_row(self, row: object) -> Thread:
        try:
            thread = FieldCodec.decode(Thread, row)
        except ValueError as error:
            raise InvalidRegistryDocument(error) from error
        return self._binding(thread)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys((*self._raw, *self._decoded)))

    def __len__(self) -> int:
        return len(self._raw.keys() | self._decoded.keys())

    def __contains__(self, name: object) -> bool:
        return name in self._raw or name in self._decoded

    def __setitem__(self, name: str, thread: Thread) -> None:
        if thread.name != name:
            raise MalformedRecord("thread name differs from its declaration")
        self._decoded[name] = self._binding(thread)
        self._raw.pop(name, None)

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._raw.pop(name, None)
        self._decoded.pop(name, None)

    def to_wire(self) -> dict[str, JsonValue]:
        return {**self._raw, **{name: FieldCodec.encode(thread)
                               for name, thread in self._decoded.items()}}

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        """The stored rows are Thread records keyed by name, whatever is decoded yet."""
        return {"type": "object", "additionalProperties": FieldCodec.record_schema(Thread)}

    @classmethod
    def checked_rows(cls, raw: object) -> dict[str, JsonValue]:
        if not isinstance(raw, dict):
            raise MalformedRecord("registry threads must be an object")
        for name, row in raw.items():
            if not isinstance(name, str) or not isinstance(row, dict) or row.get("name") != name:
                raise MalformedRecord("thread name differs from its declaration")
        return dict(raw)

    @classmethod
    def from_wire(cls, raw: object) -> Self:
        result = cls()
        result._raw = cls.checked_rows(raw)
        return result

    @classmethod
    def from_threads(cls, threads: Mapping[str, Thread]) -> Self:
        result = cls()
        result.update(threads)
        return result

    def copy(self) -> ThreadRows:
        result = ThreadRows()
        result._raw = self._raw.copy()
        result._decoded = self._decoded.copy()
        result._binding = self._binding
        return result

    def freeze(self) -> ThreadRows:
        result = FrozenThreadRows()
        result._raw = self._raw.copy()
        result._decoded = self._decoded.copy()
        result._binding = self._binding
        return result

    def reuse(self, rows: Mapping[str, object], kept: frozenset[str], earlier: ThreadRows) -> None:
        self._raw = self.checked_rows(rows)
        self._decoded = {name: earlier._decoded[name] for name in kept if name in earlier._decoded}
        self._binding = earlier._binding


class FrozenThreadRows(ThreadRows):
    def __setitem__(self, name: str, thread: Thread) -> None:
        raise TypeError("snapshot thread rows are read-only")

    def __delitem__(self, name: str) -> None:
        raise TypeError("snapshot thread rows are read-only")


class ThreadRowsSlice(OwnRow[ThreadRows]):
    @classmethod
    def copy(cls, value: ThreadRows) -> ThreadRows:
        return value.copy()

    @classmethod
    def freeze(cls, value: ThreadRows) -> ThreadRows:
        return value.freeze()

    @classmethod
    def reuse(cls, rows: Mapping[str, object], kept: frozenset[str],
              earlier: ThreadRows, changed: ThreadRows) -> None:
        changed.reuse(rows, kept, earlier)


@dataclass(frozen=True)
class ThreadSection:
    """A document field holding one row per thread name. Its field plan owns both the
    wire key and the attribute, so a revision reuses unchanged rows without naming them."""

    plan: FieldPlan

    def rows(self, raw: Mapping[str, object]) -> dict[str, object]:
        rows = raw.get(self.plan.key)
        if not isinstance(rows, dict):
            raise MalformedRecord(f"registry section {self.plan.key} must be an object")
        return rows

    def decoded(self, document: object) -> MutableMapping[str, object]:
        rows = self.plan.current(document)
        if not isinstance(rows, MutableMapping):
            raise TypeError(f"{self.plan.name} does not hold one row per thread")
        return rows

    def kept(self, raw: Mapping[str, object], earlier_raw: Mapping[str, object],
             earlier: object) -> frozenset[str]:
        """Names whose row equals the earlier revision's, so their decoded value is reused."""
        held, before = self.decoded(earlier), self.rows(earlier_raw)
        return frozenset(
            name for name, row in self.rows(raw).items()
            if name in held and name in before and before[name] == row
        )

    def changed_rows(self, raw: Mapping[str, object], kept: frozenset[str]) -> dict[str, object]:
        return {name: row for name, row in self.rows(raw).items() if name not in kept}

    def reuse(self, raw: Mapping[str, object], kept: frozenset[str], earlier: object,
              changed: object) -> None:
        """Fill ``changed``'s own rows, in wire order, with the earlier decoded value of
        every kept name; ``changed`` decoded only the rows that differ."""
        kind = ThreadSlice.of(self.plan)
        if not issubclass(kind, OwnRow):
            raise TypeError(f"{self.plan.name} does not hold reusable rows")
        kind.reuse(self.rows(raw), kept, self.plan.current(earlier), self.plan.current(changed))


class AliasesOf(MappingRowsSlice):
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


class WholeDocumentValue[Value](SnapshotValue[Value]):
    """A scalar shared by the whole document and every one-thread view."""

    @staticmethod
    def cut(raw: Value, canonical: str) -> Value:
        return raw

    @classmethod
    def copy(cls, value: Value) -> Value:
        return value

    @classmethod
    def freeze(cls, value: Value) -> Value:
        return cls.copy(value)


class RootPurpose(DeclaredFamily, affix="RootPurpose"):
    """The registry's durable permission to mutate this root."""

    @abstractmethod
    def require_write(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkingRootPurpose(RootPurpose):
    def require_write(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class HistoricalRootPurpose(RootPurpose):
    def require_write(self) -> None:
        raise RelationViolationError("Historical roots are read-only")


@dataclass(frozen=True)
class CommanderProject(DeclaredFamily, affix="CommanderProject"):
    """This bus's commander declaration; identity resolves through the registry lookup."""

    @abstractmethod
    def commanders(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]: ...

    @abstractmethod
    def repository_names(self) -> tuple[RepositoryName, ...]: ...

    @abstractmethod
    def require(self) -> RegisteredCommanderProject: ...

    @abstractmethod
    def named(self, commander: ThreadIncarnation,
              repositories: RepositorySelection) -> RegisteredCommanderProject: ...

    @abstractmethod
    def reconfigured(self, repositories: RepositorySelection) -> CommanderProject: ...


class NoCommanderProject(CommanderProject):
    def commanders(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        return ()

    def repository_names(self) -> tuple[RepositoryName, ...]:
        return ()

    def require(self) -> RegisteredCommanderProject:
        raise RelationViolationError("This bus has no registered commander project.")

    def named(self, commander: ThreadIncarnation,
              repositories: RepositorySelection) -> RegisteredCommanderProject:
        return RegisteredCommanderProject(commander, repositories.founding(commander.name))

    def reconfigured(self, repositories: RepositorySelection) -> CommanderProject:
        repositories.without_commander()
        return self


@dataclass(frozen=True)
class RegisteredCommanderProject(CommanderProject):
    commander: ThreadIncarnation
    repos: tuple[Repo, ...]

    def commanders(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        selected = snapshot.threads_for(self.commander.require_recorded())
        if not selected:
            raise RelationViolationError("The registered commander thread no longer exists.")
        return selected

    def repository_names(self) -> tuple[RepositoryName, ...]:
        return tuple(repo.github for repo in self.repos)

    def require(self) -> RegisteredCommanderProject:
        return self

    def named(self, commander: ThreadIncarnation,
              repositories: RepositorySelection) -> RegisteredCommanderProject:
        return RegisteredCommanderProject(commander, repositories.applied(self.repos))

    def reconfigured(self, repositories: RepositorySelection) -> CommanderProject:
        return replace(self, repos=repositories.applied(self.repos))


@ThreadSlice.required
@dataclass
class RegistryDocument(RegistryPresence[dict[str, ThreadStatus]]):
    version: Annotated[
        Literal[1], SlicePolicy(WholeDocumentValue), WireRequired()
    ] = 1
    purpose: Annotated[RootPurpose, SlicePolicy(WholeDocumentValue)] = field(
        default_factory=WorkingRootPurpose
    )
    commander_project: Annotated[
        CommanderProject, SlicePolicy(WholeDocumentValue), WireOmitDefault()
    ] = field(default_factory=NoCommanderProject)
    threads: Annotated[ThreadRows, SlicePolicy(ThreadRowsSlice)] = field(
        default_factory=ThreadRows
    )
    statuses: Annotated[dict[str, ThreadStatus], SlicePolicy(MappingRowsSlice)] = field(
        default_factory=dict
    )
    last_seen: Annotated[dict[str, float], SlicePolicy(MappingRowsSlice)] = field(
        default_factory=dict
    )
    aliases: Annotated[dict[str, str], SlicePolicy(AliasesOf)] = field(
        default_factory=dict
    )
    owners: Annotated[GenerationCounter, SlicePolicy(GenerationOf)] = field(
        default_factory=GenerationCounter
    )
    claims: Annotated[
        GenerationCounter, SlicePolicy(GenerationOf)
    ] = field(default_factory=GenerationCounter)

    @cached_property
    def _human(self) -> UserThreadLookup[str]:
        # This answer belongs to the decoded revision. Keep the name, not an
        # unbound Thread: store binding and later reads use the current row.
        return next((FoundUserThread(human.name)
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
                if key in raw:
                    canonical = kind.canonical(raw[key], canonical)
            restricted = {key: kind.cut(raw[key], canonical) for kind, key in keyed if key in raw}
        except (AttributeError, KeyError, TypeError) as error:
            raise InvalidRegistryDocument(error) from error
        return cls.from_wire(restricted).entry(name)

    @classmethod
    def thread_sections(cls) -> tuple[ThreadSection, ...]:
        """The sections that hold one row per thread name."""
        return tuple(
            ThreadSection(plan)
            for plan in FieldCodec.field_plans(cls)
            if plan.declared.init and plan.records and issubclass(ThreadSlice.of(plan), OwnRow)
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
                if not isinstance(raw, dict):
                    raise MalformedRecord("registry document must be an object")
                kept = tuple(
                    (section, section.kept(raw, earlier_raw, decoded))
                    for section in cls.thread_sections()
                )
                changed = FieldCodec.decode(cls, {**raw, **{
                    section.plan.key: section.changed_rows(raw, same) for section, same in kept
                }})
                for section, same in kept:
                    section.reuse(raw, same, decoded, changed)
                document = changed
            names = set(document.threads)
            if names != set(document.statuses) or names != set(document.last_seen):
                raise MalformedRecord("thread presence does not match declarations")
            if not names <= document.owners.generations.keys():
                raise MalformedRecord("missing owner generation")
            if not names <= document.claims.generations.keys():
                raise MalformedRecord("missing claim_scope generation")
            if any(
                target not in names or alias in names for alias, target in document.aliases.items()
            ):
                raise MalformedRecord("alias has no exclusive retained owner")
            return document
        except MalformedRecord as error:
            raise InvalidRegistryDocument(error) from error

    def observe_offered_tools(self, identity: ThreadIncarnation, offered: OfferedTools) -> Thread:
        current = self.require(identity.name)
        if current.incarnation != identity:
            raise RelationViolationError("Tool observation belongs to a replaced thread.")
        updated = current.observing_tools(offered)
        self.threads[current.name] = updated
        return updated

    def set_work_state(self, name: str, state: ThreadWorkState) -> Thread:
        """Change order availability without replacing the thread's owner or presence."""
        current = self.require(name)
        if not current.work_state.may_become(state):
            raise RelationViolationError("Invalid thread work-state transition.")
        updated = replace(current, work_state=state)
        self.threads[current.name] = updated
        return updated

    def set_commander_project(self, project: CommanderProject) -> CommanderProject:
        """Publish a project only for its current registered commander identity."""
        for thread in project.commanders(RegistrySnapshot.capture(self)):
            thread.require_commander()
        self.commander_project = project
        return project

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
        used = {existing.created_at for existing in self.threads.values()}
        thread = replace(thread, created_at=thread.created_at.distinct_from(used))

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
        available = ChainMap(restored, self.threads)
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
            if child.parent.named(canonical):
                self.threads[child_name] = replace(child, parent=ForkedParent(new_name))
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
        self.status(name).withdraw_owner(self.owners, name)
        thread = self.threads[name]
        self.set_status(name, thread.execution.registered_status(thread, StoppedThreadStatus()))
        self.advance_claim(name)
        self.threads[name] = thread.without_turn()

    def archive(self, name: str) -> None:
        name = self.require(name).name
        self.status(name).withdraw_owner(self.owners, name)
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
            sorted(child.name for child in self.threads.values() if child.parent.named(name))
        )
        for child_name in detached:
            self.threads[child_name] = replace(self.threads[child_name], parent=NoParent())
        self.status(name).withdraw_owner(self.owners, name)
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
        if current.turn_identity != canonical_lease.identity:
            return False, NoFinishedTurn()
        turn = record.read().require_active()
        if not turn.may_release(canonical_lease):
            return False, NoFinishedTurn()
        attested = (canonical_lease.can_attest(self.claims.generations[name])
                    and turn.held_by(canonical_lease))
        completion = (self.status(name).completed_turn(canonical_lease)
                      if attested else NoFinishedTurn())
        keeps_resume = turn.phase.keeps_project_resume
        record.without_claim()
        self.threads[name] = replace(
            current,
            turn_record=FinishedTurn(lease.turn_id),
            project_resume_due=current.project_resume_due and keeps_resume,
        )
        self.last_seen[name] = time.time()
        return True, completion


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
        return self.status.owns_entry(self, owner, process)

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


@SnapshotValue.required
@dataclass(frozen=True, slots=True)
class RegistrySnapshot(RegistryPresence[Mapping[str, ThreadStatus]], RegistryProvenance):
    purpose: Annotated[RootPurpose, SlicePolicy(WholeDocumentValue)]
    threads: Annotated[ThreadRows, SlicePolicy(ThreadRowsSlice)]
    statuses: Annotated[Mapping[str, ThreadStatus], SlicePolicy(MappingRowsSlice)]
    last_seen: Annotated[Mapping[str, float], SlicePolicy(MappingRowsSlice)]
    aliases: Annotated[Mapping[str, str], SlicePolicy(MappingRowsSlice)]
    owner_generations: Annotated[Mapping[str, int], SlicePolicy(MappingRowsSlice)]
    _claimed_generations: Annotated[Mapping[str, int], SlicePolicy(MappingRowsSlice)] = field(repr=False)
    commander_project: Annotated[CommanderProject, SlicePolicy(WholeDocumentValue)] = field(
        default_factory=NoCommanderProject
    )
    def __post_init__(self) -> None:
        """Own each copied value and derive the birth index from the frozen threads."""
        for plan in FieldCodec.field_plans(type(self)):
            if plan.declared.init:
                object.__setattr__(
                    self, plan.name, SnapshotValue.of(plan).freeze(getattr(self, plan.name)),
                )
        # ThreadRows validates names when rows enter; aliases belong to this cut.
        self.require_alias_names()

    @cached_property
    def by_lookup(self) -> Mapping[str, Thread]:
        indexed: dict[str, Thread] = {}
        for thread in self.threads.values():
            lookup = thread.incarnation.require_recorded().lookup
            if lookup in indexed:
                raise RelationViolationError("Registry creation identities collide.")
            indexed[lookup] = thread
        return MappingProxyType(indexed)

    def __reduce__(self) -> tuple[type[Self], tuple[object, ...]]:
        """Worker restoration preserves scalar policy and re-enters the constructor."""
        return type(self), tuple(
            SnapshotValue.of(plan).copy(getattr(self, plan.name))
            for plan in FieldCodec.field_plans(type(self)) if plan.declared.init
        )

    def claim_generation(self, name: str) -> int:
        """The allocated input generation follows this registry's alias lineage."""
        return self._claimed_generations[self.canonical_name(name)]

    @property
    def known_names(self) -> tuple[str, ...]:
        """Every declared name, including live, renamed and deleted participants."""
        return tuple(dict.fromkeys((*self._claimed_generations, *self.aliases)))

    @property
    def retired_names(self) -> tuple[str, ...]:
        """Deleted declarations keep their generation without exposing its store."""
        return tuple(name for name in self._claimed_generations if name not in self.threads)

    def threads_for(self, incarnation: RecordedThreadIncarnation) -> tuple[Thread, ...]:
        return (
            (self.threads[self.canonical_name(incarnation.name)],)
            if incarnation.current(self) else ()
        )

    def holds_turn(self, identity: TurnIdentity) -> bool:
        """Whether its thread still names this turn generation; a superseded lease does not."""
        return any(thread.turn_identity == identity
                   for thread in self.threads_for(identity.incarnation.require_recorded()))

    def current_turn(self, held: StoredTurnRecord) -> TurnRecord:
        """What the held record's thread names now: the record itself, or its successor once a
        newer lease superseded it; a thread no longer here leaves the held record to refuse."""
        return next((thread.turn_record for thread in
                     self.threads_for(held.identity.incarnation.require_recorded())), held)

    def idle_after(self, fence: FinishedTurnFence) -> tuple[Thread, ...]:
        return tuple(
            thread
            for thread in self.active_threads(self.threads_for(fence.identity.incarnation.require_recorded()))
            if thread.latest_idle_turn(fence, self.claim_generation(thread.name))
        )

    @classmethod
    def capture(cls, document: RegistryDocument) -> RegistrySnapshot:
        """One live registry cut owns the index and all its source maps."""
        return cls(
            purpose=document.purpose,
            threads=document.threads,
            statuses=document.statuses,
            last_seen=document.last_seen,
            aliases=document.aliases,
            owner_generations=document.owners.generations,
            _claimed_generations=document.claims.generations,
            commander_project=document.commander_project,
        )

    def observed_activity(
        self, agents: AgentActivity, names: Iterable[str],
    ) -> dict[str, ObservedActivity | DeletedThread]:
        """Observe retained view names; absent declarations have no execution authority."""
        selected = {name: self.canonical_name(name) for name in names}
        captured = agents.all_activity(
            self.visible_threads(DEFAULT_ROSTER, names=selected), snapshot=self)
        observed: dict[str, ObservedActivity | DeletedThread] = {}
        for name, canonical in selected.items():
            if canonical in captured:
                observed[name] = captured[canonical]
            elif canonical not in self.threads:
                observed[name] = DeletedThread(canonical)
        return observed

    def unread_since(
        self, actor: str, since: float | None, recorded_delivery: bool,
    ) -> float | None:
        return self.require(actor).role.unread_since(since, recorded_delivery)

    def displayed_recipient_effect(self, reads, message, thread: Thread, *, document=None):
        return thread.role.displayed_recipient_effect(reads, message, thread, self, document)

    def recipient(
        self, recipient: FrozenRecipient, original: Delivered
    ) -> RegisteredRecipient | RetiredRecipient:
        """A missing live thread was retired only if the original send names it."""
        lookup = recipient.recipient_lookup
        if lookup in self.by_lookup:
            thread = self.by_lookup[lookup]
            if thread.name != self.canonical_name(recipient.canonical_thread):
                raise RelationViolationError("Frozen recipient name does not match its lookup.")
            return RegisteredRecipient(thread)
        if recipient not in original.recipients:
            raise UnregisteredThreadError("Recipient lookup is not in the original message.")
        return RetiredRecipient(lookup)

    def retired(self, name: str) -> bool:
        """Removal keeps the name's generations; they are the deletion record."""
        return name in self.retired_names

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
        return ClaimedIdentity(self.threads[canonical].incarnation, self.claim_generation(canonical))

    def same_authority(self, name: str, other: RegistrySnapshot, other_name: str) -> bool:
        """One participant retains its owner, input claim and registered presence.

        Titles, aliases and unrelated rows do not allocate or revoke authority.
        """
        return (
            self.authority(name) == other.authority(other_name)
            and self.status(name) == other.status(other_name)
        )

    def authority(self, name: str) -> ThreadAuthority:
        return ThreadAuthority(self.owner_identity(name), self.claimed_identity(name))

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
        if self.claim_generation(expected.name) != claim_scope:
            raise RelationViolationError("Stopping owner claim_scope changed")
