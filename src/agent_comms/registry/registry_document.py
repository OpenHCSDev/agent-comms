from copy import copy

from agent_comms.cli_verbs.target_command_bindings import TargetCommandBindings
from agent_comms.messaging.claim_registry_read import ClaimRegistryRead
from agent_comms.registry.activity_registry_reads import ActivityRegistryRead
from agent_comms.registry.presence_reads import PresenceEntry, PresenceSnapshot

"""One registry document: thread facts, identity domains and lifecycle behavior."""


import time
from abc import ABC, abstractmethod
from collections import ChainMap
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
    Set,
)
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import JsonValue

from agent_comms.errors import OutsideRegistrySlice, RelationViolationError, UnregisteredThreadError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_ingress import InputRegistry
from agent_comms.execution.native_input_owner import NativeOwnerRegistry
from agent_comms.execution.restart_refusals import (
    OwnerBusyRefusal,
    OwnerChangedBeforeFenceRefusal,
    OwnerGenerationChangedRefusal,
)
from agent_comms.execution.turn_lease import (
    FinishedTurn,
    FinishedTurnFence,
    LockedTurn,
    NamedTurn,
    NoFinishedTurn,
    RetiredTurn,
    StoredTurnRecord,
    TurnCompletion,
    TurnLeaseFence,
    TurnRecord,
)
from agent_comms.messaging.channel_targets import ResolvedTarget, SurroundingChannel, TeammatesTag
from agent_comms.messaging.channels import Channel, ChannelMemberIndex
from agent_comms.messaging.delivery_recipients import DeliveryRecipients
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.mentions import ThreadMention
from agent_comms.messaging.snapshot_slices import (
    OwnRow,
    SharedRowsSlice,
    SnapshotRestored,
    SnapshotValue,
    ThreadSlice,
    WholeDocumentValue,
)
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    ExternalJsonShape,
    FieldCodec,
    MalformedRecord,
    SlicePolicy,
    TaggedRecordMember,
    TaggedUnion,
    ThreadLookup,
    WireOmitDefault,
    WireRequired,
    WireTransient,
    WireValue,
)
from agent_comms.projects.change_report import CitationProject
from agent_comms.projects.checkouts import Checkout, RepositoryReads
from agent_comms.projects.repo import Repo, RepositorySelection
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.activity import ActivityCapture, ObservedActivity
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.presence import PresenceCapture, PresenceRowsSlice
from agent_comms.registry.registration_change import (
    InitialRegistration,
    RegistrationChange,
    UpdatedRegistration,
)
from agent_comms.registry.registry_names import DeletedThread
from agent_comms.registry.registry_provenance import (
    RegisteredRecipient,
    RegistryProvenance,
    RetiredRecipient,
)
from agent_comms.registry.roster_visibility import DEFAULT_ROSTER, RosterVisibility
from agent_comms.registry.task_registry import TaskRegistry
from agent_comms.registry.thread_creation import CreationIdentityCollisionError, CreationTime
from agent_comms.registry.thread_identity import (
    ClaimedIdentity,
    GenerationCounter,
    OfferedTools,
    OwnerIdentity,
    RecordedThreadIncarnation,
    ThreadAuthority,
    ThreadIncarnation,
    ThreadIncarnationRecord,
)
from agent_comms.registry.thread_presentation import ThreadOwnerBinding
from agent_comms.registry.thread_reads import ThreadOwnerSource
from agent_comms.registry.thread_status import (
    ActiveThreadPresence,
    ArchivedThreadStatus,
    StoppedThreadStatus,
    StoppingThreadStatus,
    ThreadStatus,
)
from agent_comms.registry.thread_text import ForkedParent, NoParent
from agent_comms.registry.threads import Thread, ThreadWorkState
from agent_comms.registry.user_thread_lookup import FoundUserThread, NoUserThread, UserThreadLookup
from agent_comms.storage.row_mapping import ReadOnlyRows, RowKeys, RowMapping
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError


@dataclass(frozen=True)
class ResolvedThread(ResolvedTarget):
    thread: Thread
    status: ThreadStatus

    def bindings[Context, C: TargetCommandBindings[Context, Thread, ThreadStatus, SurroundingChannel, Checkout, RegistrySnapshot, CatalogAccess, ResolvedChannel]](
        self, declaration: type[C], comms: Context, channel: SurroundingChannel,
        *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess,
    ) -> tuple[C, ...]:
        return declaration.thread_bindings(
            comms, self.thread, self.status, channel, catalog=catalog,
        )


@dataclass(frozen=True)
class ResolvedChannel(ResolvedTarget):
    channel: Channel
    members: tuple[ResolvedThread, ...]

    def bindings[Context, C: TargetCommandBindings[Context, Thread, ThreadStatus, SurroundingChannel, Checkout, RegistrySnapshot, CatalogAccess, ResolvedChannel]](
        self, declaration: type[C], comms: Context, channel: SurroundingChannel,
        *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess,
    ) -> tuple[C, ...]:
        return declaration.channel_bindings(
            comms, self, project=project, snapshot=snapshot, catalog=catalog,
        )


class ProjectThreadSelection(TaggedUnion, ToolResult, affix="ProjectThread"):
    """The registry's complete answer, including absence and ambiguity. A remote UI receives
    it from Core, so each record carries its kind."""


    @classmethod
    def from_threads(cls, threads: tuple[Thread, ...]) -> ProjectThreadSelection:
        if not threads:
            return NoProjectThread()
        if len(threads) == 1:
            return FoundProjectThread(threads[0])
        return SeveralProjectThreads(threads)

    def name_or(self, fallback: str) -> str:
        """Keep the caller's existing identity unless one thread was selected."""
        return fallback


@dataclass(frozen=True)
class NoProjectThread(TaggedRecordMember, ProjectThreadSelection):
    """There is no declared ACP thread at this project."""


@dataclass(frozen=True)
class FoundProjectThread(TaggedRecordMember, ProjectThreadSelection):
    thread: Thread

    def name_or(self, fallback: str) -> str:
        return self.thread.name


@dataclass(frozen=True)
class SeveralProjectThreads(TaggedRecordMember, ProjectThreadSelection):
    threads: tuple[Thread, ...]


class RegistryPresence[StatusMap: Mapping[str, ThreadStatus]](
    TaskRegistry[Thread], NativeOwnerRegistry, ABC,
):
    """Presence reads shared by the acquired document and its detached cut."""

    __slots__ = ()

    def require_member[Kind: Thread](self, name: str, kind: type[Kind]) -> Kind:
        """The registered declaration, narrowed to the member an operation needs."""
        return kind.stored(self.require(name))

    def __contains__(self, name: str) -> bool:
        """A thread is present under its name or any alias the namespace keeps for it."""
        return self.canonical_name(name) in self.threads

    threads: ThreadRows
    statuses: StatusMap
    last_seen: Mapping[str, float]

    def named_incarnations(self) -> Iterator[tuple[str, ThreadIncarnation]]:
        """Every current and retained name with its identity. A UI asks on each registry
        revision, and the revision changes constantly, so this decodes no row."""
        for name in self.threads:
            yield name, self.threads.incarnation(name)
        for alias in self.aliases:
            yield alias, self.threads.incarnation(self.canonical_name(alias))

    def children(self, name: str) -> tuple[Thread, ...]:
        return self.threads.children(self.canonical_name(name))

    def active_candidates(self) -> Iterator[Thread]:
        return (self.threads[name] for name, status in self.statuses.items()
                if status.may_be_active())

    def set_status(self: RegistryPresence[dict[str, ThreadStatus]], name: str, status: ThreadStatus) -> None:
        self.statuses[name] = status.declaration()

    def status(self, name: str) -> ThreadStatus:
        thread = self.require(name)
        return self.statuses[thread.name].registered(thread, self.last_seen[thread.name])

    def resolve_thread(self, name: str) -> ResolvedThread:
        """Bind a thread and its status from this same acquired registry."""
        thread = self.require(name)
        return ResolvedThread(thread, self.status(thread.name))

    def resolve_channel(self, channel: Channel) -> ResolvedChannel:
        """A channel's visible members and statuses share this one registry cut."""
        members = tuple(self.resolve_thread(thread.name)
                        for thread in self.channel_audience()
                        if channel.matches(thread.tags))
        return ResolvedChannel(channel, members)

    def receiving(self, name: str) -> bool:
        """Whether a message sent now is read now; an unregistered name reads nothing."""
        return name in self.threads and self.status(name).is_receiving()

    def stopped(self, names: Iterable[str]) -> frozenset[str]:
        """The registered threads among ``names`` that read nothing sent now; a channel or an
        unregistered name is not a stopped thread."""
        return frozenset(name for name in names
                         if name in self.threads and not self.receiving(name))

    def require_active(self, name: str) -> Thread:
        try:
            thread = self.require(name)
            self.status(thread.name).require_active()
        except UnregisteredThreadError as error:
            raise RelationViolationError("live owner is stopped or unavailable") from error
        return thread

    def seen_at(self, name: str) -> float:
        return self.last_seen[self.require(name).name]

    @abstractmethod
    def owner_generation(self, name: str) -> int: ...


    def entry(self, name: str) -> RegistryEntry:
        thread = self.require(name)
        return RegistryEntry(
            thread=thread,
            status=self.status(thread.name),
            last_seen=self.last_seen[thread.name],
            owner_generation=self.owner_generation(thread.name),
            claimed_generation=self.claim_generation(thread.name),
            names=self.names_for(thread.name),
        )

    def roster(self, visibility: RosterVisibility) -> Iterator[Thread]:
        """Every thread whose status this roster shows, executing or not: the user is listed;
        archived and service threads are not, by their statuses' own in_view."""
        return (self.threads[name] for name, status in self.statuses.items()
                if status.in_view(visibility))

    def visible_threads(
        self, visibility: RosterVisibility, *, names: Iterable[str] | None = None,
    ) -> Iterator[Thread]:
        """The roster's threads that execute turns, or only the named ones among them."""
        selected = (self.roster(visibility) if names is None else
                    (self.threads[name] for name in dict.fromkeys(map(self.canonical_name, names))
                     if name in self.threads and self.status(name).in_view(visibility)))
        return (executor for thread in selected for executor in thread.role.executor(thread))

    def mentions(self, body: str) -> tuple[ThreadMention, ...]:
        """The live threads the body @-names; any other @word stays plain text."""
        return ThreadMention.find(body, lambda name: next(
            (thread.name for thread in self.visible_threads(DEFAULT_ROSTER, names=(name,))), None))

    def active_threads(self, candidates: Iterable[Thread]) -> Iterator[Thread]:
        """Select activity-bearing presences from this already acquired registry cut."""
        return (member for thread in candidates
                for member in self.status(thread.name).items_with(ActiveThreadPresence, thread))

    def channel_audience(self) -> Iterator[Thread]:
        """A channel reaches the default roster: stopped owners receive, archived do not."""
        return self.visible_threads(DEFAULT_ROSTER)

    @cached_property
    def _human(self) -> UserThreadLookup[str]:
        # Names belong to this revision; resolution uses its bound thread rows.
        return next((FoundUserThread(human.name)
                     for thread in self.threads.values()
                     for human in thread.role.human(thread)), NoUserThread())

    def user_thread(self) -> UserThreadLookup[Thread]:
        return self._human.map(self.require)


class InvalidRegistryDocument(RelationViolationError):
    def __init__(self, error: Exception) -> None:
        super().__init__(f"Invalid registry document: {error}")


class ThreadRow(ABC):
    """One declaration owns its stored value and its lazily decoded value."""

    @property
    @abstractmethod
    def thread(self) -> Thread: ...

    @property
    @abstractmethod
    def wire(self) -> dict[str, JsonValue]: ...

    @property
    def birth(self) -> CreationTime:
        return self.thread.created_at

    def matches_parent(self, parent: str) -> bool:
        return self.thread.parent.named(parent)

    @abstractmethod
    def rebound(self, binding: Callable[[Thread], Thread]) -> ThreadRow: ...

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ThreadRow):
            return NotImplemented
        return self is other or self.same(other)

    def same(self, other: ThreadRow) -> bool:
        return self.thread == other.thread

    def same_stored(self, other: StoredThreadRow) -> bool:
        return self.thread == other.thread


@dataclass(eq=False)
class StoredThreadRow(ThreadRow):
    row: dict[str, JsonValue]
    binding: Callable[[Thread], Thread]

    @cached_property
    def thread(self) -> Thread:
        try:
            return self.binding(FieldCodec.decode(Thread, self.row))
        except ValueError as error:
            raise InvalidRegistryDocument(error) from error

    @property
    def wire(self) -> dict[str, JsonValue]:
        return self.row

    @cached_property
    def birth(self) -> CreationTime:
        try:
            return CreationTime.from_wire(self.row.get(FieldCodec.record_key(Thread, "created_at")))
        except ValueError as error:
            raise InvalidRegistryDocument(error) from error

    def matches_parent(self, parent: str) -> bool:
        return self.row.get(FieldCodec.record_key(Thread, "parent")) == FieldCodec.encode(ForkedParent(parent))

    def rebound(self, binding: Callable[[Thread], Thread]) -> ThreadRow:
        return self if self.binding == binding else StoredThreadRow(self.row, binding)

    def same(self, other: ThreadRow) -> bool:
        return other.same_stored(self)

    def same_stored(self, other: StoredThreadRow) -> bool:
        return self.row == other.row or self.thread == other.thread


@dataclass(eq=False)
class AssignedThreadRow(ThreadRow):
    value: Thread

    @property
    def thread(self) -> Thread:
        return self.value

    @cached_property
    def wire(self) -> dict[str, JsonValue]:
        return FieldCodec.encode(self.thread)

    def rebound(self, binding: Callable[[Thread], Thread]) -> ThreadRow:
        return AssignedThreadRow(binding(self.thread))


class ThreadRows(ExternalJsonShape, WireValue, MutableMapping[str, Thread]):
    """A revision's thread rows, decoded and bound only when read."""

    def __init__(self) -> None:
        self._rows: RowMapping[str, ThreadRow] = RowMapping()
        self._birth_counts: RowMapping[float, int] = RowMapping()
        self._unreadable_births: RowMapping[str, ThreadRow] = RowMapping()
        self._binding: Callable[[Thread], Thread] = self.unbound

    @staticmethod
    def unbound(thread: Thread) -> Thread:
        return thread

    def bind(self, binding: Callable[[Thread], Thread]) -> None:
        if self._binding == binding:
            return
        self._rows = RowMapping({name: row.rebound(binding) for name, row in self._rows.items()})
        self._binding = binding

    def __eq__(self, other: object) -> bool:
        """Related revisions compare their writes; unchanged cells share their decode.
        Rows equal only rows: a plain mapping is a different value."""
        if not isinstance(other, ThreadRows):
            return NotImplemented
        return self._rows == other._rows

    def differing(self, other: ThreadRows) -> frozenset[str]:
        """The names whose rows differ from ``other``'s, added and removed ones included."""
        return self._rows.differing(other._rows)

    def changed_in_order(self, earlier: ThreadRows) -> tuple[str, ...]:
        return self._rows.changed_in_order(earlier._rows)

    def children(self, parent: str) -> tuple[Thread, ...]:
        """Select the declared parent before decoding any complete thread row."""
        return tuple(row.thread for row in self._rows.values() if row.matches_parent(parent))

    def __getitem__(self, name: str) -> Thread:
        return self._rows[name].thread

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, name: object) -> bool:
        return name in self._rows

    def __setitem__(self, name: str, thread: Thread) -> None:
        if thread.name != name:
            raise MalformedRecord("thread name differs from its declaration")
        self._put_row(name, AssignedThreadRow(self._binding(thread)))

    def _adjust_birth(self, birth: CreationTime, amount: int) -> None:
        before = self._birth_counts.get(birth, 0)
        after = before + amount
        if after:
            self._birth_counts[birth] = after
        else:
            del self._birth_counts[birth]

    def _index_birth(self, name: str, row: ThreadRow, amount: int) -> None:
        try:
            birth = row.birth.distinct_from(frozenset())
        except (InvalidRegistryDocument, CreationIdentityCollisionError):
            # Retain the source, not an exception with a growing traceback. A local
            # entry read does not need another row's creation metadata to decode.
            if amount > 0:
                self._unreadable_births[name] = row
            else:
                del self._unreadable_births[name]
        else:
            self._adjust_birth(birth, amount)

    def _put_row(self, name: str, row: ThreadRow) -> None:
        if name in self._rows:
            self._index_birth(name, self._rows[name], -1)
        self._index_birth(name, row, 1)
        self._rows[name] = row

    def __delitem__(self, name: str) -> None:
        row = self._rows[name]
        del self._rows[name]
        self._index_birth(name, row, -1)

    def clear(self) -> None:
        self._rows.clear()
        self._birth_counts.clear()
        self._unreadable_births.clear()

    def popitem(self) -> tuple[str, Thread]:
        name = self._rows.any_key()
        thread = self[name]
        del self[name]
        return name, thread

    def wire_at(self, name: str) -> dict[str, JsonValue]:
        return self._rows[name].wire

    def to_wire(self) -> dict[str, JsonValue]:
        """Export all rows, each retaining its own cached encoding.

        The complete export walks the mapping; incremental commits must select
        changed keys before asking their cells for wire values.
        """
        return {name: row.wire for name, row in self._rows.items()}

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        """The stored rows are Thread records keyed by name, whatever is decoded yet."""
        return {"type": "object", "additionalProperties": FieldCodec.record_schema(Thread)}

    @classmethod
    def checked_rows(cls, raw: object) -> dict[str, dict[str, JsonValue]]:
        if not isinstance(raw, dict):
            raise MalformedRecord("registry threads must be an object")
        for name, row in raw.items():
            if not isinstance(name, str) or not isinstance(row, dict) or row.get("name") != name:
                raise MalformedRecord("thread name differs from its declaration")
        return dict(raw)

    def birth(self, name: str) -> CreationTime:
        """One row's creation time, read alone: a stored row is never decoded for it."""
        return self._rows[name].birth

    def _require_births(self) -> None:
        if self._unreadable_births:
            row = self._unreadable_births[self._unreadable_births.any_key()]
            row.birth.distinct_from(frozenset())
            raise RelationViolationError("Registry birth data changed outside its row owner.")

    def names(self) -> RowKeys[str]:
        return self._rows.keyset()

    def births(self) -> Set[float]:
        self._require_births()
        return self._birth_counts.keys()

    def incarnation(self, name: str) -> ThreadIncarnation:
        """One row's identity, read from its name and birth alone."""
        return ThreadIncarnationRecord(name, self.birth(name).to_wire()).incarnation()

    def lookup_names(self) -> dict[ThreadLookup, str]:
        """Each row's creation lookup, read from its birth alone: resolving one owner never
        decodes the other threads."""
        return {self.incarnation(name).require_recorded().lookup: name for name in self}

    def require_distinct_births(self) -> None:
        """Rows decode late, so a read of one thread never sees two sharing a creation lookup."""
        self._require_births()
        if len(self._rows) != len(self._birth_counts):
            raise CreationIdentityCollisionError()

    @classmethod
    def from_wire(cls, raw: object) -> Self:
        result = cls()
        for name, row in cls.checked_rows(raw).items():
            result._put_row(name, StoredThreadRow(row, result._binding))
        return result

    @classmethod
    def from_threads(cls, threads: Mapping[str, Thread]) -> Self:
        result = cls()
        result.update(threads)
        return result

    def _copy_into(self, result: ThreadRows, rows: RowMapping[str, ThreadRow]) -> ThreadRows:
        result._rows = rows
        result._birth_counts = self._birth_counts.copy()
        result._unreadable_births = self._unreadable_births.copy()
        result._binding = self._binding
        return result

    def copy(self) -> ThreadRows:
        return self._copy_into(ThreadRows(), self._rows.copy())

    def only(self, names: frozenset[str]) -> ThreadRows:
        """The selected rows retain their shared decode and wire values."""
        result = ThreadRows()
        result._binding = self._binding
        for name, row in self._rows.only(names).items():
            result._put_row(name, row)
        return result

    def freeze(self) -> ThreadRows:
        return self._copy_into(FrozenThreadRows(), self._rows.freeze())

    def reuse(self, rows: Mapping[str, object], changed_names: Collection[str], earlier: ThreadRows) -> None:
        updated = earlier.copy()
        for name in changed_names:
            if name in rows:
                updated._put_row(name, self._rows[name].rebound(earlier._binding))
            else:
                del updated[name]
        updated._copy_into(self, updated._rows)


class FrozenThreadRows(ReadOnlyRows[str, Thread], ThreadRows):
    """The thread view composes the same read-only capability as other row views."""


class ThreadRowsSlice(OwnRow[ThreadRows]):
    @classmethod
    def copy(cls, value: ThreadRows) -> ThreadRows:
        return value.copy()

    @classmethod
    def freeze(cls, value: ThreadRows) -> ThreadRows:
        return value.freeze()

    @classmethod
    def reuse(cls, rows: Mapping[str, object], changed_names: Collection[str],
              earlier: ThreadRows, changed: ThreadRows) -> ThreadRows:
        changed.reuse(rows, changed_names, earlier)
        return changed

    @classmethod
    def only(cls, value: ThreadRows, names: frozenset[str]) -> ThreadRows:
        return value.only(names)

    @classmethod
    def differing(cls, earlier: ThreadRows, current: ThreadRows) -> frozenset[str]:
        return current.differing(earlier)


class GenerationOf(ThreadSlice[GenerationCounter]):
    """An editable generation counter with its own generations mapping."""

    @classmethod
    def copy(cls, value: GenerationCounter) -> GenerationCounter:
        following = copy(value)
        following.generations = SharedRowsSlice.copy(value.generations)
        return following

    @classmethod
    def wire_changes(cls, current: object, earlier: object) -> object:
        plan = FieldCodec.field_plan(GenerationCounter, "generations")
        if not isinstance(current, Mapping) or not isinstance(earlier, Mapping):
            raise MalformedRecord("registry generation counter must be an object")
        default = FieldCodec.encode(plan.default())
        return {**current, plan.key: SharedRowsSlice.wire_changes(
            current.get(plan.key, default), earlier.get(plan.key, default))}

    @classmethod
    def refreshed(cls, current: object, previous: object,
                  earlier: GenerationCounter, decoded: GenerationCounter) -> GenerationCounter:
        plan = FieldCodec.field_plan(GenerationCounter, "generations")
        if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
            raise MalformedRecord("registry generation counter must be an object")
        default = FieldCodec.encode(plan.default())
        following = copy(decoded)
        following.generations = SharedRowsSlice.refreshed(
            current.get(plan.key, default), previous.get(plan.key, default),
            earlier.generations, decoded.generations)
        # A reduced allocation ceiling changes every retained value's validity.
        # Normal monotonic updates only validate their decoded changed rows.
        if following.counter < earlier.counter:
            return FieldCodec.decode(GenerationCounter, FieldCodec.encode(following))
        return following


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
class TeamProject(DeclaredFamily, CitationProject, affix="TeamProject"):
    """This bus's teammate declaration; identity resolves through the registry lookup."""

    def participants(self, threads: Iterable[Thread]) -> tuple[Thread, ...]:
        """An undeclared project has no participants."""
        return ()

    def colleagues(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        """Independent colleagues, derived from their execution declarations."""
        return self.participants(snapshot.threads.values())

    def require_actor(self, snapshot: RegistrySnapshot,
                      actor: ThreadIncarnation) -> RegisteredTeamProject:
        """Validate the actual birth and its declared independent or in-turn caller binding."""
        project = self.require()
        own = snapshot.born(actor.require_recorded().lookup)
        candidates = (own, *snapshot.threads_for(actor.acting_identity.require_recorded()))
        if not project.participants(candidates):
            raise ToolRefusedError(
                "Only registered independent colleagues or their bound in-turn children "
                "may act on this project.")
        return project

    def require_scope(self, snapshot: RegistrySnapshot, actor: ThreadIncarnation,
                      repository: RepositoryName) -> RegisteredTeamProject:
        """The shared actor and configured-repository boundary for review and integration."""
        project = self.require_actor(snapshot, actor)
        if repository not in project.repository_names():
            raise ToolRefusedError(
                f"Repository {repository.to_wire()} is outside this project's configured scope.")
        return project

    def boards(self) -> tuple[RegisteredTeamProject, ...]:
        """The one team board this bus keeps: none without a registered project."""
        return ()

    def unique_repositories(self, name: str = '') -> tuple[Repo, ...]:
        return ()

    def linkable(self, primary: RepositoryName) -> tuple[RepositoryName, ...]:
        """The repositories an order on ``primary`` may link pull requests in: its own and
        each repository this project registers."""
        return (primary, *self.repository_names())

    @abstractmethod
    def require(self) -> RegisteredTeamProject: ...

    def registered_repos(self) -> tuple[Repo, ...]:
        return self.require().repos

    @abstractmethod
    def reconfigured(self, repositories: RepositorySelection) -> TeamProject: ...


class NoTeamProject(TeamProject):
    def repository_names(self, name: str = "") -> tuple[RepositoryName, ...]:
        return ()

    def require(self) -> RegisteredTeamProject:
        raise ToolRefusedError("This bus has no registered team project.")

    def checkout_named(self, name: str) -> Path:
        """No registered checkout to name: the written directory stands, and a relative one is
        refused where the check is written."""
        return Path(name)

    def reconfigured(self, repositories: RepositorySelection) -> TeamProject:
        """Given repositories found the team; no thread is recorded as its lead."""
        return repositories.founded(self, RegisteredTeamProject)


@dataclass(frozen=True)
class RegisteredTeamProject(TeamProject):
    """The team's board, orders and repositories: they belong to the team, not to a thread."""

    repos: tuple[Repo, ...]

    def participants(self, threads: Iterable[Thread]) -> tuple[Thread, ...]:
        """Execution owns independence; tags and fork provenance grant none."""
        return tuple(dict.fromkeys(
            peer for thread in threads
            for peer in thread.role.independent_threads(thread)))

    def boards(self) -> tuple[RegisteredTeamProject, ...]:
        return (self,)

    def repository_names(self, name: str = "") -> tuple[RepositoryName, ...]:
        """Each selected GitHub repository once, however many checkouts point at it."""
        return tuple(repo.github for repo in self.unique_repositories(name))

    def unique_repositories(self, name: str = '') -> tuple[Repo, ...]:
        """The first configured checkout owns each forge identity, without a wire round trip."""
        selected: dict[RepositoryName, Repo] = {}
        for repo in Repo.candidates(self.repos, name):
            selected.setdefault(repo.github, repo)
        return tuple(selected.values())

    def require(self) -> RegisteredTeamProject:
        return self

    def reconfigured(self, repositories: RepositorySelection) -> TeamProject:
        return replace(self, repos=repositories.applied(self.repos))


@ThreadSlice.required
@dataclass
class RegistryDocument(RegistryPresence[MutableMapping[str, ThreadStatus]]):
    version: Annotated[
        Literal[3], SlicePolicy(WholeDocumentValue), WireRequired()
    ] = 3
    purpose: Annotated[RootPurpose, SlicePolicy(WholeDocumentValue)] = field(
        default_factory=WorkingRootPurpose
    )
    team_project: Annotated[
        TeamProject, SlicePolicy(WholeDocumentValue), WireOmitDefault()
    ] = field(default_factory=NoTeamProject)
    threads: Annotated[ThreadRows, SlicePolicy(ThreadRowsSlice)] = field(
        default_factory=ThreadRows
    )
    statuses: Annotated[MutableMapping[str, ThreadStatus], SlicePolicy(SharedRowsSlice)] = field(
        default_factory=dict
    )
    last_seen: Annotated[MutableMapping[str, float], SlicePolicy(PresenceRowsSlice), WireTransient()] = field(
        default_factory=dict, compare=False
    )
    """Presence, stored by PresenceStore and laid over each revision as it is read: a
    heartbeat that changes only this leaves the document equal, so registry.json is not
    rewritten for it."""
    aliases: Annotated[MutableMapping[str, str], SlicePolicy(SharedRowsSlice)] = field(
        default_factory=dict
    )
    owners: Annotated[GenerationCounter, SlicePolicy(GenerationOf)] = field(
        default_factory=GenerationCounter
    )
    claims: Annotated[
        GenerationCounter, SlicePolicy(GenerationOf)
    ] = field(default_factory=GenerationCounter)

    def __post_init__(self) -> None:
        for plan in FieldCodec.field_plans(type(self)):
            if plan.declared.init:
                plan.assign(self, ThreadSlice.of(plan).owned(plan.current(self)))

    def copy(self) -> RegistryDocument:
        return replace(
            self,
            **{
                plan.name: ThreadSlice.of(plan).copy(plan.current(self))
                for plan in FieldCodec.field_plans(type(self))
                if plan.declared.init
            },
        )

    def require_valid_rows(self, earlier: RegistryDocument | None = None) -> None:
        """Validate changed memberships against a valid earlier document; cold reads check all."""
        try:
            aliases: Iterable[str] = self.aliases
            if earlier is None:
                changed = frozenset(self.threads).union(self.statuses)
            else:
                membership = self.threads.names().differing(earlier.threads.names())
                changed = membership
                for current, before in (
                    (self.statuses, earlier.statuses),
                    (self.owners.generations, earlier.owners.generations),
                    (self.claims.generations, earlier.claims.generations),
                ):
                    changed = changed | SharedRowsSlice.owned(current).keyset().differing(
                        SharedRowsSlice.owned(before).keyset())
                # Only removal can orphan an unchanged alias target. Added names can
                # collide with an alias directly, without enumerating other aliases.
                if all(name in self.threads for name in membership):
                    aliases = SharedRowsSlice.differing(earlier.aliases, self.aliases) | membership
            for name in changed:
                present = name in self.threads
                if present != (name in self.statuses):
                    raise MalformedRecord("thread statuses do not match declarations")
                if present and name not in self.owners.generations:
                    raise MalformedRecord("missing owner generation")
                if present and name not in self.claims.generations:
                    raise MalformedRecord("missing claim_scope generation")
            if self.has_invalid_aliases(aliases):
                raise MalformedRecord("alias has no exclusive retained owner")
        except MalformedRecord as error:
            raise InvalidRegistryDocument(error) from error

    def presented(self, captured: PresenceCapture) -> RegistryDocument:
        """Retain a scoped view; continuing coverage checks inspect new names alone."""
        try:
            seen = PresenceRowsSlice.owned(self.last_seen).presented(captured, self.threads.names())
        except MalformedRecord as error:
            raise InvalidRegistryDocument(error) from error
        return replace(self, last_seen=seen)

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

    def owner_generation(self, name: str) -> int:
        return self.owners.generations[self.canonical_name(name)]

    def claim_generation(self, name: str) -> int:
        return self.claims.generations[self.canonical_name(name)]

    def entry_if_present(self, name: str) -> RegistryEntry | None:
        return self.entry(name) if self.canonical_name(name) in self.threads else None

    @classmethod
    def from_wire(cls, raw: dict, earlier: tuple[Mapping[str, object], RegistryDocument] | None = None
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
                plans = tuple(plan for plan in FieldCodec.field_plans(cls)
                              if plan.declared.init and plan.records)
                defaults = {plan.key: FieldCodec.encode(plan.default()) for plan in plans}
                narrowed = {**raw, **{
                    plan.key: ThreadSlice.of(plan).wire_changes(
                        raw[plan.key], earlier_raw.get(plan.key, defaults[plan.key]))
                    for plan in plans if plan.key in raw
                }}
                changed = FieldCodec.decode(cls, narrowed)
                for plan in plans:
                    plan.assign(changed, ThreadSlice.of(plan).refreshed(
                        raw.get(plan.key, defaults[plan.key]),
                        earlier_raw.get(plan.key, defaults[plan.key]),
                        plan.current(decoded), plan.current(changed)))
                document = changed
            document.require_valid_rows(earlier[1] if earlier is not None else None)
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

    def select_checkout(
        self, identity: ThreadIncarnation, rule: StorageRule, repository: RepositoryName,
        path: Path,
    ) -> Thread:
        """Change only this incarnation's repository choice for one selectable checkout."""
        current = self.require(identity.name)
        if current.incarnation != identity:
            raise RelationViolationError("Checkout owner changed before selection")
        rule.require_selectable_checkout(current, path, repository, RepositoryReads())
        updated = replace(current, storage_roots=rule.roots_of(current).choosing(repository, path))
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

    def set_team_project(self, project: TeamProject) -> TeamProject:
        """Publish a project for its registered teammate identity."""
        self.team_project = project
        return project

    def prepare_declaration(self, thread: Thread, status: ThreadStatus) -> RegistrationChange:
        canonical = self.canonical_name(thread.name)
        requested = thread.for_registration(canonical, self.threads.get(canonical))
        change = self.prepare_registration(requested, status, new_owner=False)
        return change.declared(requested)

    def prepare_registration(
        self, thread: Thread, status: ThreadStatus, *, new_owner: bool
    ) -> RegistrationChange:
        status = thread.registered_status(status)
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
        thread = replace(thread, created_at=thread.created_at.distinct_from(self.threads.births()))

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
            additions.append(thread.without_process())
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
        for child in self.children(canonical):
            self.threads[child.name] = replace(child, parent=ForkedParent(new_name))
        for alias, target in tuple(self.aliases.items()):
            if target == canonical:
                self.aliases[alias] = new_name
        self.aliases[canonical] = new_name
        self.claims.rename(canonical, new_name)
        self.owners.rename(canonical, new_name)
        return canonical, new_name

    def fence_restart_owner(self, expected: Thread, *, expected_claimed_generation: int) -> int:
        """Atomically deny new turns for exactly one idle or wedged owner before signaling.

        The outer wire lock alone cannot exclude a direct registry claim; this
        check and the STOPPED transition share the registry's own lock. A wedged
        turn's reference is dropped here exactly as stop's fence drops it.
        """
        current = self.require_active(expected.name)
        try:
            current.require_restartable()
        except RelationViolationError as error:
            raise OwnerBusyRefusal() from error
        if current != expected:
            raise OwnerChangedBeforeFenceRefusal()
        if self.claims.generations[current.name] != expected_claimed_generation:
            raise OwnerGenerationChangedRefusal()
        self.set_status(expected.name, expected.registered_status(StoppingThreadStatus()))
        self.advance_claim(expected.name)
        self.owners.advance(expected.name)
        self.threads[expected.name] = current.without_turn()
        return self.claims.generations[expected.name]

    def unregister(self, name: str) -> None:
        """Stopped; the execution decides whether the row keeps its process binding."""
        thread = self._withdraw(name, StoppedThreadStatus())
        self.threads[thread.name] = thread.withdrawn()

    def fence_owner(self, name: str) -> None:
        """Refuse new work while the host completes the session; the binding stays."""
        self._withdraw(name, StoppingThreadStatus())

    def _withdraw(self, name: str, status: ThreadStatus) -> Thread:
        name = self.require(name).name
        self.status(name).require_mutable(name)
        self.status(name).withdraw_owner(self.owners, name)
        thread = self.threads[name]
        self.set_status(name, thread.registered_status(status))
        self.advance_claim(name)
        self.threads[name] = thread.without_turn()
        return self.threads[name]

    def archive(self, name: str) -> None:
        name = self.require(name).name
        self.status(name).require_mutable(name)
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
        for original in originals:
            self.status(original.name).require_deleting()
        return {original.name: self._remove(original.name) for original in originals}

    def begin_delete(self, name: str) -> None:
        name = self.require(name).name
        self.set_status(name, self.status(name).for_deletion())
        self.advance_claim(name)

    def remove(self, name: str) -> tuple[str, ...]:
        """Withdraw a mutable declaration; an in-progress deletion owns its completion."""
        self.status(name).require_mutable(name)
        return self._remove(name)

    def _remove(self, name: str) -> tuple[str, ...]:
        """Remove a declaration and atomically detach its surviving children."""
        name = self.require(name).name
        detached = tuple(
            sorted(child.name for child in self.children(name))
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
        keeps_resume = turn.phase.keeps_project_resume()
        record.without_claim()
        self.threads[name] = replace(
            current,
            turn_record=FinishedTurn(lease.turn_id),
            project_resume_due=current.project_resume_due and keeps_resume,
        )
        self.last_seen[name] = time.time()
        return True, completion


@dataclass(frozen=True, slots=True)
class RegistryEntry(PresenceEntry):
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
class RegistrySnapshot(
    SnapshotRestored,
    RegistryPresence[Mapping[str, ThreadStatus]],
    RegistryProvenance,
    ToolResult,
    ThreadOwnerSource[Thread],
    InputRegistry[Thread],
    PresenceSnapshot,
    ActivityRegistryRead,
    ClaimRegistryRead[Thread],
):
    purpose: Annotated[RootPurpose, SlicePolicy(WholeDocumentValue)]
    threads: Annotated[ThreadRows, SlicePolicy(ThreadRowsSlice)]
    statuses: Annotated[Mapping[str, ThreadStatus], SlicePolicy(SharedRowsSlice)]
    last_seen: Annotated[Mapping[str, float], SlicePolicy(PresenceRowsSlice)]
    aliases: Annotated[Mapping[str, str], SlicePolicy(SharedRowsSlice)]
    owner_generations: Annotated[Mapping[str, int], SlicePolicy(SharedRowsSlice)]
    _claimed_generations: Annotated[Mapping[str, int], SlicePolicy(SharedRowsSlice)] = field(repr=False)
    team_project: Annotated[TeamProject, SlicePolicy(WholeDocumentValue)] = field(
        default_factory=NoTeamProject
    )
    def __post_init__(self) -> None:
        """Own each copied value and derive the birth index from the frozen threads."""
        for plan in FieldCodec.field_plans(type(self)):
            if plan.declared.init:
                object.__setattr__(
                    self, plan.name, SnapshotValue.of(plan).freeze(plan.current(self)),
                )
        # ThreadRows validates names when rows enter; aliases and births belong to this cut.
        self.require_alias_names()
        self.threads.require_distinct_births()

    @cached_property
    def lookup_names(self) -> Mapping[ThreadLookup, str]:
        return MappingProxyType(self.threads.lookup_names())

    def only(self, names: Collection[str]) -> RegistrySlice:
        """The slice a caller reading ``names`` needs: the rows each one's presentation reads,
        beside every shared field. A remote UI opening one thread
        reads this, not the whole document, almost all of which is other threads' rows."""
        kept = frozenset().union(*(self.read_by(name) for name in names))
        # The snapshot's own fields, sliced; the slice adds only what it was asked for. Plans of
        # type(self) would already hold a slice's ``asked`` when a slice is sliced again.
        fields = [ThreadSlice.of(plan).only(plan.current(self), kept)
                  for plan in FieldCodec.field_plans(RegistrySnapshot)
                  if plan.declared.init]
        fields.append(frozenset(names))
        return RegistrySlice(*fields)

    def changed_names(self, earlier: RegistrySnapshot) -> frozenset[str]:
        """The names whose registry facts differ from ``earlier``'s, by each field's declared
        slice: an own-row field answers its differing rows, a shared field answers every name.
        A turn start rewrites one thread's rows, so it names that thread alone."""
        every = frozenset((*self.threads, *earlier.threads, *self.aliases, *earlier.aliases))
        return frozenset().union(*(
            ThreadSlice.of(plan).changed_names(
                plan.current(earlier), plan.current(self), every)
            for plan in FieldCodec.field_plans(type(self)) if plan.declared.init
        ))

    def claimed_readers(self, names: Iterable[str]) -> tuple[str, ...]:
        """Each of ``names`` whose status claims a reader of its inbox."""
        return tuple(reader for name in names for reader in self.status(name).claimed_readers(name))

    def changed_names_to(self, later: RegistrySnapshot) -> frozenset[str]:
        """What a reader that last read this cut must re-read at ``later``."""
        return later.changed_names(self)

    def read_by(self, name: str) -> frozenset[str]:
        """The names whose rows ``name``'s presentation reads: its own, the alias it was asked
        by, and every parent whose history a fork reads. The walk goes up only, because a
        presentation reads no child's row; one that shows its forks must walk down."""
        seen: set[str] = set()
        pending = [name, self.canonical_name(name)]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in self.threads:
                pending.extend(self.threads[current].parent.names())
        return frozenset(seen)

    def affected_by(self, name: str, changed: frozenset[str]) -> bool:
        """Whether a change to ``changed`` reaches ``name``'s presentation."""
        return not self.read_by(name).isdisjoint(changed)

    def born(self, lookup: ThreadLookup) -> Thread:
        """The one thread a creation lookup names; only its row decodes."""
        try:
            name = self.lookup_names[lookup]
        except KeyError as error:
            raise UnregisteredThreadError(f"Thread lookup {lookup!r} is not registered.") from error
        return self.threads[name]

    def owner_generation(self, name: str) -> int:
        return self.owner_generations[self.canonical_name(name)]

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

    def teammates_channel(self) -> tuple[Thread, ...]:
        """Every member of #teammates, forks included: each may answer for any teammate."""
        return tuple(ChannelMemberIndex(self.threads.values())[TeammatesTag.declared_name])

    def threads_for(self, incarnation: RecordedThreadIncarnation) -> tuple[Thread, ...]:
        return (
            (self.threads[self.canonical_name(incarnation.name)],)
            if incarnation.current(self) else ()
        )

    def locked_turn(self, record: StoredTurnRecord) -> LockedTurn:
        """The held turn with the owner this cut names, while its thread still names that turn
        generation; a superseded lease finds a retired turn."""
        return next((NamedTurn(record, self.owner_identity(thread.name))
                     for thread in self.threads_for(record.identity.incarnation.require_recorded())
                     if thread.turn_identity == record.identity), RetiredTurn())

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
            team_project=document.team_project,
        )

    def observed_activity(
        self, agents: AgentActivity, names: Iterable[str],
    ) -> ActivityCapture[ObservedActivity | DeletedThread]:
        """Observe retained view names; absent declarations have no execution authority."""
        selected = {name: self.canonical_name(name) for name in names}
        captured = agents.captured(
            self.visible_threads(DEFAULT_ROSTER, names=selected), snapshot=self)
        observed: dict[str, ObservedActivity | DeletedThread] = {}
        for name, canonical in selected.items():
            if canonical in captured.activity:
                observed[name] = captured.activity[canonical]
            elif canonical not in self.threads:
                observed[name] = DeletedThread(canonical)
        return ActivityCapture(observed, captured.expires_at)

    def unread_since(
        self, actor: str, since: float | None, recorded_delivery: bool,
    ) -> float | None:
        return self.require(actor).role.unread_since(since, recorded_delivery)

    def displayed_recipient_effect(self, reads, message, thread: Thread, *, document=None):
        return thread.role.displayed_recipient_effect(reads, message, thread, self, document)

    def recipient(
        self, recipient: FrozenRecipient, original: DeliveryRecipients
    ) -> RegisteredRecipient | RetiredRecipient:
        """A missing live thread was retired only if the original send names it."""
        lookup = recipient.recipient_lookup
        if lookup in self.lookup_names:
            thread = self.born(lookup)
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

    def process_owners(self, process: ProcessIdentity) -> tuple[str, ...]:
        return tuple(thread.name for thread in self.threads.values()
                     if thread.process_identity == process and self.status(thread.name).holds_process())

    def owner_identity(self, name: str) -> OwnerIdentity:
        canonical = self.canonical_name(name)
        return OwnerIdentity(self.threads[canonical].incarnation, self.owner_generations[canonical])

    def claimed_identity(self, name: str) -> ClaimedIdentity:
        canonical = self.canonical_name(name)
        return ClaimedIdentity(
            self.threads[canonical].incarnation, self.claim_generation(canonical))

    def same_thread(self, actual: Thread, other: RegistrySnapshot, expected: Thread) -> bool:
        """Keep one launch, turn and authority across preparation; ignore display metadata."""
        return (
            actual.same_launch(expected)
            and actual.same_turn(expected)
            and self.same_authority(actual.name, other, expected.name)
        )

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
        return thread.owner_binding(self)

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


@dataclass(frozen=True, slots=True)
class RegistrySlice(RegistrySnapshot):
    """The rows a caller asked for, beside every shared field. A name it asked for and did not
    get is unregistered; a name it never asked for is outside the slice, so its refusal says
    which."""

    asked: Annotated[frozenset[str], SlicePolicy(WholeDocumentValue)] = frozenset()

    def unregistered(self, name: str) -> ToolRefusedError:
        if name in self.asked:
            return super().unregistered(name)
        return OutsideRegistrySlice(
            f"Thread {name!r} is not in this registry slice; ask for it by name.")


class UnreadRegistry:
    """A reader that has read no registry cut yet: every thread is new to it."""

    def changed_names_to(self, later: RegistrySnapshot) -> frozenset[str]:
        return frozenset((*later.threads, *later.aliases))
