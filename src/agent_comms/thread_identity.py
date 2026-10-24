"""A7: thread provenance, process ownership and turn identity are distinct facts.

Names are resolved by the registry before constructing an identity. A restart
does not create a new historical thread; a new turn does not change its owner.
"""

from __future__ import annotations

from .class_fact import ClassFact
import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import TYPE_CHECKING, Annotated, ClassVar, Self, TypeVar

from agent_comms.participant_identity import ParticipantIdentity

from .cli_options import JsonFlagRecord
from .declared_family import (
    DeclaredFamily,
    UniqueExternalSelector,
    UnknownFamilyMember,
    declared_words,
)
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    TurnId,
    SingleFieldValue,
    WireName,
    WireValue,
    ThreadLookup,
)
from .projection_source import ProducedRow, ProjectionSource
from .response_policy import CollectivePolicy, InformationalPolicy, ResponsePolicy
from .wake_policy import MessageAudience

if TYPE_CHECKING:
    from .child_process import ProcessIdentity
    from .messages import Message
    from .read_ledger import ReadDocument, ReadLedger
    from .registry_provenance import RegistryProvenance
    from .threads import Thread
    from .todos import Todo

T = TypeVar("T")


@dataclass(frozen=True, slots=True, order=True)
class ToolName(SingleFieldValue):
    """A name supplied by the runtime's offered-tool declaration."""

    value: str


class OfferedTools(DeclaredFamily, affix="OfferedTools"):
    """No observation, a reported catalogue, or an explicitly unreported catalogue."""

    @abstractmethod
    def changes_from(self, previous: OfferedTools) -> tuple[str, ...]: ...

    @abstractmethod
    def before_reported(self, current: ReportedTools) -> tuple[str, ...]: ...

    @abstractmethod
    def before_unreported(self, current: UnreportedTools) -> tuple[str, ...]: ...

    @abstractmethod
    def on_version(self, version: str) -> tuple[OfferedTools, ...]: ...

    @abstractmethod
    def restarted_over(self, before: OfferedTools) -> OfferedTools: ...

    def on_catalogue(self, names: frozenset[ToolName]) -> tuple[OfferedTools, ...]:
        return ()

    def without_catalogue(self) -> tuple[OfferedTools, ...]:
        return ()

    def fills_version(self, version: str) -> bool:
        return False

    def filled_by(self, current: OfferedTools) -> bool:
        return False


@dataclass(frozen=True)
class UnobservedTools(OfferedTools):
    """No runtime has reported yet; this member never produces a publication."""

    def changes_from(self, previous: OfferedTools) -> tuple[str, ...]:
        return ()

    def before_reported(self, current: ReportedTools) -> tuple[str, ...]:
        return ()

    def before_unreported(self, current: UnreportedTools) -> tuple[str, ...]:
        return ()

    def on_version(self, version: str) -> tuple[OfferedTools, ...]:
        return (UnreportedTools(version),)

    def restarted_over(self, before: OfferedTools) -> OfferedTools:
        return before  # Nothing observed: the last real record stays the comparison.

    def filled_by(self, current: OfferedTools) -> bool:
        return True


@dataclass(frozen=True)
class ObservedTools(OfferedTools, declared_name=None):
    version: str

    def unreported_change(self, version: str) -> tuple[str, ...]:
        return (
            (f"runtime {self.version} -> {version}, tool set unreported",)
            if self.version != version
            else ()
        )

    def before_unreported(self, current: UnreportedTools) -> tuple[str, ...]:
        return self.unreported_change(current.version)

    def on_version(self, version: str) -> tuple[OfferedTools, ...]:
        return (UnreportedTools(version),) if self.version != version else ()

    def on_catalogue(self, names: frozenset[ToolName]) -> tuple[OfferedTools, ...]:
        return (ReportedTools(self.version, names),)

    def without_catalogue(self) -> tuple[OfferedTools, ...]:
        return (UnreportedTools(self.version),)

    def fills_version(self, version: str) -> bool:
        return self.version == version

    def restarted_over(self, before: OfferedTools) -> OfferedTools:
        return self


@dataclass(frozen=True)
class ReportedTools(ObservedTools):
    # Not ``names``: dataclass would take the inherited DeclaredFamily.names as its default.
    tools: Annotated[frozenset[ToolName], WireName("names")]

    def changes_from(self, previous: OfferedTools) -> tuple[str, ...]:
        return previous.before_reported(self)

    def before_reported(self, current: ReportedTools) -> tuple[str, ...]:
        missing = self.tools - current.tools
        return (
            (
                f"missing tools {', '.join(sorted(name.value for name in missing))}; "
                f"runtime {current.version}",
            )
            if missing
            else ()
        )


class UnreportedTools(ObservedTools):
    def filled_by(self, current: OfferedTools) -> bool:
        return current.fills_version(self.version)

    def changes_from(self, previous: OfferedTools) -> tuple[str, ...]:
        return previous.before_unreported(self)

    def before_reported(self, current: ReportedTools) -> tuple[str, ...]:
        return self.unreported_change(current.version)


@dataclass(frozen=True, slots=True)
class ThreadIncarnation(JsonFlagRecord, WireValue, ABC):
    value_flag: ClassVar[str] = "--thread-incarnation"
    hands_out_released_work: ClassVar[bool] = False
    """Whether work this actor released goes to an idle unit without a commander turn."""
    name: str

    @property
    @abstractmethod
    def wire_birth(self) -> float:
        """Only the external spelling of this member's birth evidence."""

    def to_wire(self) -> dict[str, str | float]:
        return {"name": self.name, "created_at": self.wire_birth}

    @classmethod
    def schema(cls) -> dict:
        return FieldCodec.record_schema(ThreadIncarnationRecord)

    @classmethod
    def from_wire(cls, data: dict) -> ThreadIncarnation:
        return FieldCodec.decode(ThreadIncarnationRecord, data).incarnation()

    @classmethod
    def deleted(cls, name: str) -> DeletedThreadIncarnation:
        return DeletedThreadIncarnation(name)

    def __post_init__(self) -> None:
        if not FieldCodec.decode(str, self.name):
            raise ValueError("Thread incarnation requires a recorded name")

    @abstractmethod
    def require_recorded(self) -> RecordedThreadIncarnation:
        """Only the recorded member can expose a lookup capability."""

    @property
    @abstractmethod
    def birth_description(self) -> str:
        """Describe recorded or missing birth evidence without inventing a date."""

    @abstractmethod
    def renamed(self, name: str) -> ThreadIncarnation:
        """Retain this member's birth state under an original alias."""

    def resolved(self, snapshot: RegistryProvenance) -> ThreadIncarnation:
        """Follow retained rename aliases only for this exact historical owner."""
        if not self.current(snapshot):
            return self
        return snapshot.threads[snapshot.canonical_name(self.name)].incarnation

    def current(self, snapshot: RegistryProvenance) -> bool:
        thread = snapshot.threads.get(snapshot.canonical_name(self.name))
        return thread is not None and thread.incarnation == self

    def recorded_or(
        self, make: Callable[[RecordedThreadIncarnation], T], otherwise: Callable[[], T]
    ) -> T:
        return otherwise()

    @property
    def commanding_identity(self) -> ThreadIncarnation:
        return self

    def acting_for(self, authority: ThreadIncarnation) -> ThreadIncarnation:
        """This recorded assigner acting again under its order's commander.

        A birthless member (Core's watcher) carries its own authority."""
        return self

    def require_may_change(self, todo: Todo) -> None:
        todo.require_commander(self)

    def selects_lookup(self, lookup: ThreadLookup) -> bool:
        return False

    def matches_recorded_name(self, name: str, snapshot: RegistryProvenance) -> bool:
        """Relate a partial stored name to this original current incarnation.

        The registry owns aliases and birth. A stored name does not supply or
        manufacture either fact, including after a name is reused.
        """
        thread = snapshot.threads.get(snapshot.canonical_name(name))
        return thread is not None and self.resolved(snapshot) == thread.incarnation

    def recorded_names(self, snapshot: RegistryProvenance) -> tuple[ThreadIncarnation, ...]:
        """Original names retained for this exact current incarnation."""
        canonical = self.resolved(snapshot)
        return (
            canonical,
            *(
                canonical.renamed(alias)
                for alias, name in snapshot.aliases.items()
                if name == canonical.name
            ),
        )


@dataclass(frozen=True, eq=False)
class RecordedThreadIncarnation(ThreadIncarnation, ParticipantIdentity):
    """The recorded creation owns identity; names remain registry-resolved routes.

    Equality and hashing use the same birth as ``lookup``. Registry operations
    still require ``current`` / ``resolved`` to prove the original alias lineage.
    """

    name: str = field(compare=False)
    created_at: float

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ThreadIncarnation):
            return NotImplemented
        return self.wire_birth == other.wire_birth

    def __hash__(self) -> int:
        return hash(self.wire_birth)

    @property
    def wire_birth(self) -> float:
        return self.created_at

    @classmethod
    def from_wire(cls, data: dict) -> RecordedThreadIncarnation:
        return ThreadIncarnation.from_wire(data).require_recorded()

    def require_recorded(self) -> RecordedThreadIncarnation:
        return self

    def recorded_or(
        self, make: Callable[[RecordedThreadIncarnation], T], otherwise: Callable[[], T]
    ) -> T:
        return make(self)

    def selects_lookup(self, lookup: ThreadLookup) -> bool:
        return self.lookup == lookup

    def acting_for(self, authority: ThreadIncarnation) -> CommanderForkIdentity:
        return CommanderForkIdentity(self.name, self.created_at, authority)

    @property
    def birth_description(self) -> str:
        return f"created {self.created_at}"

    def renamed(self, name: str) -> RecordedThreadIncarnation:
        return RecordedThreadIncarnation(name, self.created_at)

    def __post_init__(self) -> None:
        super().__post_init__()
        birth = FieldCodec.decode(float, self.created_at)
        if not math.isfinite(birth) or birth <= 0:
            raise ValueError("Thread incarnation requires a positive recorded birth")

    @cached_property
    def lookup(self) -> ThreadLookup:
        """The creation lookup, computed once for this recorded incarnation."""
        identity = f"agent-comms:thread-creation:v1\0{float(self.created_at)!r}".encode()
        return ThreadLookup(hashlib.sha256(identity).hexdigest()[:32])


@dataclass(frozen=True, eq=False)
class CommanderForkIdentity(RecordedThreadIncarnation):
    """A registered actor's parent authority, resolved before any Todo transaction.

    The ordinary incarnation wire form records the actor, not a reusable authority.
    """

    authority: ThreadIncarnation

    @property
    def commanding_identity(self) -> ThreadIncarnation:
        return self.authority


class BirthlessIncarnation(
    ThreadIncarnation, UniqueExternalSelector, DeclaredFamily, affix="Incarnation"
):
    """A member whose wire birth is a declared mark, not recorded evidence; never a lookup.

    Each member declares its mark in ``WIRE_BIRTH``; the record decodes a mark to its member."""

    WIRE_BIRTH: ClassVar[float]

    @classmethod
    def selection_key(cls) -> float:
        return cls.WIRE_BIRTH

    @property
    def wire_birth(self) -> float:
        return self.WIRE_BIRTH

    def require_recorded(self) -> RecordedThreadIncarnation:
        raise ValueError("Thread incarnation requires a positive recorded birth")

    def renamed(self, name: str) -> Self:
        return type(self)(name)

    @classmethod
    def recorded_as(cls, name: str) -> BirthlessIncarnation:
        """The member a stored name and this member's wire birth decode to."""
        return cls(name)


@dataclass(frozen=True)
class DeletedThreadIncarnation(BirthlessIncarnation):
    """A deleted participant retains only its name, never a lookup capability."""

    WIRE_BIRTH: ClassVar[float] = -1.0

    def current(self, snapshot: RegistryProvenance) -> bool:
        return snapshot.canonical_name(self.name) not in snapshot.threads

    def resolved(self, snapshot: RegistryProvenance) -> DeletedThreadIncarnation:
        return self

    @property
    def birth_description(self) -> str:
        return "deleted declaration"


@dataclass(frozen=True)
class UnrecordedThreadIncarnation(BirthlessIncarnation):
    """Historical zero-birth evidence cannot select a live recipient."""

    WIRE_BIRTH: ClassVar[float] = 0.0

    @property
    def birth_description(self) -> str:
        return "birth unrecorded"


@dataclass(frozen=True)
class CoreWatcherIncarnation(BirthlessIncarnation):
    """Core's own watcher: it records failed order checks, releases struck-out orders and hands
    them to idle units, and is never a thread. No clock makes it strike or release anything."""

    WIRE_BIRTH: ClassVar[float] = -2.0
    hands_out_released_work: ClassVar[bool] = True

    def require_may_change(self, todo: Todo) -> None:
        """The Core watcher may advance released orders without a commander turn."""

    @classmethod
    def watcher(cls) -> CoreWatcherIncarnation:
        """Named by its class: ``CoreWatcherIncarnation`` is ``core_watcher``."""
        return cls(declared_words(cls.__name__.removesuffix("Incarnation")))

    @classmethod
    def recorded_as(cls, name: str) -> CoreWatcherIncarnation:
        """The class owns the watcher's name; a stored spelling is never its identity."""
        return cls.watcher()

    @property
    def birth_description(self) -> str:
        return "Core's watcher"


@dataclass(frozen=True, slots=True)
class ThreadIncarnationRecord:
    """The single exact external name/birth record; members own its meaning."""

    name: str
    created_at: float

    def incarnation(self) -> ThreadIncarnation:
        try:
            member = BirthlessIncarnation.member_selected(self.created_at)
        except UnknownFamilyMember:
            return RecordedThreadIncarnation(self.name, self.created_at)
        return member.recorded_as(self.name)

    @classmethod
    def decode(cls, name: str, birth: float) -> ThreadIncarnation:
        return FieldCodec.decode(cls, {"name": name, "created_at": birth}).incarnation()




@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    """An allocation from RegistryDocument.owners, used with process proof."""

    incarnation: ThreadIncarnation
    generation: int

    def owns(self, thread: Thread, generation: int, process: ProcessIdentity) -> bool:
        return (
            self.incarnation == thread.incarnation
            and self.generation == generation
            and thread.same_local_process(process)
        )

    def require_current(self, thread: Thread, generation: int, process: ProcessIdentity) -> None:
        if self.incarnation != thread.incarnation:
            raise RelationViolationError("Thread incarnation changed during owner attachment")
        if self.generation != generation:
            raise RelationViolationError("Owner lease changed during attachment")
        thread.require_local_process(process)


@dataclass(frozen=True, slots=True)
class ClaimedIdentity:
    """A registry-allocated input generation, never an owner lease."""

    incarnation: ThreadIncarnation
    claimed_generation: int

    def includes(self, previous: ClaimedIdentity) -> bool:
        """Compare order only within this registry's input-generation counter."""
        return (
            self.incarnation == previous.incarnation
            and self.claimed_generation >= previous.claimed_generation
        )


@dataclass(frozen=True)
class ClaimScoped:
    """A record scoped to one registry-allocated input identity."""

    claimed_identity: ClaimedIdentity


@dataclass(frozen=True, slots=True)
class ThreadAuthority:
    """One registry participant's owner lease and allocated input claim."""

    owner: OwnerIdentity
    claim: ClaimedIdentity


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    incarnation: ThreadIncarnation
    generation: int

    def resolved(self, snapshot: RegistryProvenance) -> TurnIdentity:
        """Resolve the original incarnation; retain this turn allocation."""
        return replace(self, incarnation=self.incarnation.resolved(snapshot))


class ObservedTurn(ABC):
    """What a thread can witness about its current or last-completed turn."""

    @abstractmethod
    def can_attest(self, claim_scope: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class TurnFence(ObservedTurn):
    """One exact turn identity and its recorded input generation."""

    identity: TurnIdentity
    turn_id: TurnId
    claimed_generation: int

    def renamed(self, name: str) -> Self:
        """Registry alias resolution changes the name, never incarnation or epochs."""
        return replace(
            self,
            identity=replace(
                self.identity, incarnation=replace(self.identity.incarnation, name=name)
            ),
        )

    def can_attest(self, claim_scope: int) -> bool:
        return (
            self.identity.generation > 0
            and self.claimed_generation > 0
            and self.claimed_generation == claim_scope
        )

    def __eq__(self, other: object) -> bool:
        """Lease and finished observations share the same turn identity."""
        if not isinstance(other, TurnFence):
            return NotImplemented
        return (
            self.identity == other.identity
            and self.turn_id == other.turn_id
            and self.claimed_generation == other.claimed_generation
        )


@dataclass(frozen=True, slots=True)
class NoObservedTurn(ObservedTurn):
    """No turn has run on this thread; it attests nothing and matches no fence."""

    def can_attest(self, claim_scope: int) -> bool:
        return False


@dataclass(slots=True)
class GenerationCounter(ProjectionSource):
    """One monotonic allocation domain, including deleted-name tombstones."""

    counter: int = 0
    generations: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            type(self.counter) is not int
            or self.counter < 0
            or any(
                type(name) is not str or type(value) is not int or not 0 < value <= self.counter
                for name, value in self.generations.items()
            )
        ):
            raise ValueError("invalid registry generation counter")

    @staticmethod
    def require_positive(value: int) -> int:
        value = FieldCodec.decode(int, value)
        if value < 1:
            raise ValueError("live owner requires a positive generation")
        return value

    def advance(self, name: str) -> int:
        self.counter += 1
        self.generations[name] = self.counter
        return self.counter

    def rename(self, old: str, new: str) -> None:
        self.generations[new] = self.generations.pop(old)


class ThreadRole(DeclaredFamily, ProjectionSource, affix="ThreadRole"):
    """Whether a thread executes turns is the member's type; its wire name is its spelling."""

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from .bus_activity_index import BusActivityIndex
        from .bus_display_index import BusDisplayIndex
        return (BusActivityIndex, BusDisplayIndex)


    origin_label: ClassVar[str]

    @classmethod
    def acp_thread[T](cls, thread: T) -> tuple[T, ...]:
        """Only the declared ACP role contributes to project session discovery."""
        return ()

    @classmethod
    @abstractmethod
    def initial_delivery(cls, message: Message, target: str) -> tuple[Message, list[str]]: ...

    @classmethod
    @abstractmethod
    def require_executable(cls, refusal: Exception) -> None: ...

    @classmethod
    @abstractmethod
    def require_user(cls, refusal: Exception) -> None: ...

    @classmethod
    @abstractmethod
    def executor[T](cls, item: T) -> tuple[T, ...]:
        """The item when its thread executes turns, else nothing."""

    @classmethod
    @abstractmethod
    def human[T](cls, item: T) -> tuple[T, ...]:
        """The item when its thread is a human reader, else nothing."""

    @classmethod
    @abstractmethod
    def unread_since(cls, since: float | None, recorded_delivery: bool) -> float | None:
        """A routed display's unread boundary, never permission to start an input."""

    @classmethod
    @abstractmethod
    def displayed_recipient_effect(
        cls,
        reads: ReadLedger,
        message: Message,
        thread: Thread,
        snapshot: RegistryProvenance,
        document: ReadDocument | None,
    ) -> object:
        """Only a human recipient's display records a read."""

    @classmethod
    @abstractmethod
    def page_human_sender(cls) -> bool:
        """The bus page row's stored human-sender column."""

    @classmethod
    @abstractmethod
    def channel_policy(cls) -> ResponsePolicy: ...

    @classmethod
    def addressed(cls, audience: MessageAudience) -> MessageAudience:
        """Who a delivery addresses: an agent's reaches only the audience its target names."""
        return audience

    @classmethod
    @abstractmethod
    def reply_target(cls, message: Message) -> str | None:
        """An external sender may receive an automatic live reply."""

    @classmethod
    @abstractmethod
    def last_user_input(cls, previous: float, timestamp: float) -> float: ...

    @classmethod
    @abstractmethod
    def user_task_facts(cls, message: Message) -> Iterable[object]: ...


class AgentThreadRole(ThreadRole):
    origin_label = "external agent"

    @classmethod
    def initial_delivery(cls, message: Message, target: str) -> tuple[Message, list[str]]:
        return message, [target]

    @classmethod
    def require_executable(cls, refusal: Exception) -> None:
        """An agent executes owner turns."""

    @classmethod
    def require_user(cls, refusal: Exception) -> None:
        raise refusal

    @classmethod
    def executor[T](cls, item: T) -> tuple[T, ...]:
        return (item,)

    @classmethod
    def human[T](cls, item: T) -> tuple[T, ...]:
        return ()

    @classmethod
    def unread_since(cls, since: float | None, recorded_delivery: bool) -> float | None:
        return since if recorded_delivery else None

    @classmethod
    def displayed_recipient_effect(
        cls,
        reads: ReadLedger,
        message: Message,
        thread: Thread,
        snapshot: RegistryProvenance,
        document: ReadDocument | None,
    ) -> object:
        return ()

    @classmethod
    def page_human_sender(cls) -> bool:
        return False

    @classmethod
    def channel_policy(cls) -> ResponsePolicy:
        return InformationalPolicy.instance()

    @classmethod
    def reply_target(cls, message: Message) -> str | None:
        return None

    @classmethod
    def last_user_input(cls, previous: float, timestamp: float) -> float:
        return previous

    @classmethod
    def user_task_facts(cls, message: Message) -> Iterable[object]:
        return ()


class AcpThreadRole(AgentThreadRole):
    """An agent created by the ACP session lifecycle, independent of channel tags."""

    @classmethod
    def acp_thread[T](cls, thread: T) -> tuple[T, ...]:
        return (thread,)


class UserThreadRole(ThreadRole):
    origin_label = "human"

    @classmethod
    def initial_delivery(cls, message: Message, target: str) -> tuple[Message, list[str]]:
        return replace(message, role=message.role.as_notice()), []

    @classmethod
    def require_executable(cls, refusal: Exception) -> None:
        raise refusal

    @classmethod
    def require_user(cls, refusal: Exception) -> None:
        """A user is the original human sender."""

    @classmethod
    def executor[T](cls, item: T) -> tuple[T, ...]:
        return ()

    @classmethod
    def human[T](cls, item: T) -> tuple[T, ...]:
        return (item,)

    @classmethod
    def unread_since(cls, since: float | None, recorded_delivery: bool) -> float | None:
        return since

    @classmethod
    def displayed_recipient_effect(
        cls,
        reads: ReadLedger,
        message: Message,
        thread: Thread,
        snapshot: RegistryProvenance,
        document: ReadDocument | None,
    ) -> object:
        return reads.displayed_incarnation(message, thread.incarnation, snapshot, document=document)

    @classmethod
    def page_human_sender(cls) -> bool:
        return True

    @classmethod
    def channel_policy(cls) -> ResponsePolicy:
        return CollectivePolicy.instance()

    @classmethod
    def addressed(cls, audience: MessageAudience) -> MessageAudience:
        """The owner addresses every member a channel reaches, so it joins their running turns."""
        return MessageAudience.MENTIONED

    @classmethod
    def reply_target(cls, message: Message) -> str | None:
        return message.exact_reply_target

    @classmethod
    def last_user_input(cls, previous: float, timestamp: float) -> float:
        return max(previous, timestamp)

    @classmethod
    def user_task_facts(cls, message: Message) -> Iterable[object]:
        return message.task.user_task_facts(message)
