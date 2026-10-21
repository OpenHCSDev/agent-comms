"""A7: thread provenance, process ownership and turn identity are distinct facts.

Names are resolved by the registry before constructing an identity. A restart
does not create a new historical thread; a new turn does not change its owner.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field, fields, replace
from functools import cached_property
from typing import TYPE_CHECKING, Callable, ClassVar, Self, TypeVar, Annotated


from agent_comms.participant_identity import ParticipantIdentity
from .agent_backend import AgentBackend, RuntimeLocation
from .channel_targets import Tag
from .cli_options import JsonFlagRecord
from .declared_family import DeclaredFamily, declared_words
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    WireValue,
    WireName,
    WireOmitDefault,
    WireRequired,
)
from .response_policy import CollectivePolicy, InformationalPolicy, ResponsePolicy
from .thread_text import NoSessionFile, NoThreadText, SessionFile, ThreadText

if TYPE_CHECKING:
    from .child_process import ProcessIdentity
    from .maintenance_barrier import MaintenanceBarrier
    from .messages import Message
    from .participant_store import ParticipantStore
    from .read_ledger import ReadDocument, ReadLedger
    from .registry_provenance import RegistryProvenance
    from .threads import Thread
    from .todos import Todo

T = TypeVar("T")


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

    def selects_lookup(self, lookup: str) -> bool:
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
        return (canonical, *(canonical.renamed(alias)
                             for alias, name in snapshot.aliases.items()
                             if name == canonical.name))


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

    def selects_lookup(self, lookup: str) -> bool:
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
    def lookup(self) -> str:
        """The creation lookup, computed once for this recorded incarnation."""
        identity = f"agent-comms:thread-creation:v1\0{float(self.created_at)!r}".encode()
        return hashlib.sha256(identity).hexdigest()[:32]


@dataclass(frozen=True, eq=False)
class CommanderForkIdentity(RecordedThreadIncarnation):
    """A registered actor's parent authority, resolved before any Todo transaction.

    The ordinary incarnation wire form records the actor, not a reusable authority.
    """

    authority: ThreadIncarnation

    @property
    def commanding_identity(self) -> ThreadIncarnation:
        return self.authority


class BirthlessIncarnation(ThreadIncarnation):
    """A member whose wire birth is a declared mark, not recorded evidence; never a lookup.

    Each member declares its mark in ``WIRE_BIRTH``; the record decodes a mark to its member."""

    WIRE_BIRTH: ClassVar[float]
    members: ClassVar[dict[float, type[BirthlessIncarnation]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.WIRE_BIRTH in BirthlessIncarnation.members:
            raise TypeError(f"Wire birth {cls.WIRE_BIRTH} is already declared")
        BirthlessIncarnation.members[cls.WIRE_BIRTH] = cls

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


@dataclass(frozen=True, kw_only=True)
class ThreadProvenance:
    """Recorded thread identity, without permission to own a process or start a turn."""

    name: str
    tags: frozenset[str]
    worktree: str
    created_at: Annotated[float, WireRequired()]
    session_file: SessionFile = field(default_factory=NoSessionFile)
    title: ThreadText = field(default_factory=NoThreadText)
    backend: Annotated[type[AgentBackend], WireOmitDefault()] = field(
        default_factory=AgentBackend.for_new_threads
    )

    @cached_property
    def runtime(self) -> RuntimeLocation:
        """The codec decoded both stored fields; their record owns this binding."""
        return self.session_file.location(self.backend)

    @property
    def display_title(self) -> str:
        return self.title.otherwise(self.name)

    @staticmethod
    def require_name(name: str) -> None:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not name or not set(name) <= allowed:
            raise ValueError(f"Thread name {name!r} must be alphanumeric with hyphens/underscores.")

    def __post_init__(self) -> None:
        self.incarnation
        self.require_name(self.name)
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")
        for tag in self.tags:
            Tag.validate(tag)

    @staticmethod
    def capture(thread: ThreadProvenance) -> ThreadProvenance:
        """Freeze only determining provenance from the original typed declaration."""
        return ThreadProvenance(
            **{
                item.name: getattr(thread, item.name)
                for item in fields(ThreadProvenance)
                if item.init
            }
        )

    @cached_property
    def incarnation(self) -> ThreadIncarnation:
        """The declaration's birth member, created once and shared by its readers."""
        return ThreadIncarnationRecord(self.name, self.created_at).incarnation()


@dataclass(frozen=True, slots=True)
class ThreadIncarnationRecord:
    """The single exact external name/birth record; members own its meaning."""

    name: str
    created_at: float

    def incarnation(self) -> ThreadIncarnation:
        if self.created_at in BirthlessIncarnation.members:
            return BirthlessIncarnation.members[self.created_at].recorded_as(self.name)
        return RecordedThreadIncarnation(self.name, self.created_at)

    @classmethod
    def decode(cls, name: str, birth: float) -> ThreadIncarnation:
        return FieldCodec.decode(cls, {"name": name, "created_at": birth}).incarnation()


@dataclass(frozen=True, slots=True)
class TurnId:
    value: str

    @classmethod
    def for_registration(cls, value: str) -> TurnId:
        value = FieldCodec.decode(str, value)
        if not 0 < len(value) <= 128:
            raise ValueError("live owner turn requires a bounded ID")
        return cls(value)

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("Turn ID cannot be empty")


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
    claimed_generation: Annotated[int, WireName("admission_generation")]

    def includes(self, previous: ClaimedIdentity) -> bool:
        """Compare order only within this registry's input-generation counter."""
        return self.incarnation == previous.incarnation and self.claimed_generation >= previous.claimed_generation


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    incarnation: ThreadIncarnation
    generation: int

    def resolved(self, snapshot: RegistryProvenance) -> TurnIdentity:
        """Resolve the original incarnation; retain this turn allocation."""
        return replace(self, incarnation=self.incarnation.resolved(snapshot))


@dataclass(frozen=True, slots=True)
class TurnFence:
    """One exact turn identity and its recorded input generation."""

    identity: TurnIdentity
    turn_id: str
    claimed_generation: Annotated[int, WireName("admission_generation")]

    def renamed(self, name: str) -> Self:
        """Registry alias resolution changes the name, never incarnation or epochs."""
        return replace(self, identity=replace(
            self.identity, incarnation=replace(self.identity.incarnation, name=name)
        ))

    def can_attest(self, claim_scope: int) -> bool:
        return (self.identity.generation > 0 and self.claimed_generation > 0
                and self.claimed_generation == claim_scope)

    def __eq__(self, other: object) -> bool:
        """Lease and finished observations share the same turn identity."""
        if not isinstance(other, TurnFence):
            return NotImplemented
        return (
            self.identity == other.identity
            and self.turn_id == other.turn_id
            and self.claimed_generation == other.claimed_generation
        )


@dataclass(slots=True)
class GenerationCounter:
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


class ThreadRole(DeclaredFamily, affix="ThreadRole"):
    """Whether a thread executes turns is the member's type; its wire name is its spelling."""

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
    def register_sender(cls, participants: ParticipantStore, lookup: str, name: str) -> None:
        """An executing sender is a participant; a human sender owns no turns to wake."""

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
    def require_maintenance_open(cls, barrier: MaintenanceBarrier, claims_owner: bool) -> None:
        """An owner claim waits for open maintenance; a human claims no owner."""

    @classmethod
    @abstractmethod
    def displayed_recipient_effect(
        cls, reads: ReadLedger, message: Message, thread: Thread,
        snapshot: RegistryProvenance, document: ReadDocument | None,
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
    def register_sender(cls, participants: ParticipantStore, lookup: str, name: str) -> None:
        participants.register(lookup, name, name, committed=True)

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
    def require_maintenance_open(cls, barrier: MaintenanceBarrier, claims_owner: bool) -> None:
        if claims_owner:
            barrier.assert_open_unlocked()

    @classmethod
    def displayed_recipient_effect(
        cls, reads: ReadLedger, message: Message, thread: Thread,
        snapshot: RegistryProvenance, document: ReadDocument | None,
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
    def register_sender(cls, participants: ParticipantStore, lookup: str, name: str) -> None:
        """A human sender owns no turns to wake."""

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
    def require_maintenance_open(cls, barrier: MaintenanceBarrier, claims_owner: bool) -> None:
        """A human claims no owner, so maintenance never gates its registration."""

    @classmethod
    def displayed_recipient_effect(
        cls, reads: ReadLedger, message: Message, thread: Thread,
        snapshot: RegistryProvenance, document: ReadDocument | None,
    ) -> object:
        return reads.displayed_incarnation(message, thread.incarnation, snapshot, document=document)

    @classmethod
    def page_human_sender(cls) -> bool:
        return True

    @classmethod
    def channel_policy(cls) -> ResponsePolicy:
        return CollectivePolicy.instance()

    @classmethod
    def reply_target(cls, message: Message) -> str | None:
        return message.exact_reply_target

    @classmethod
    def last_user_input(cls, previous: float, timestamp: float) -> float:
        return max(previous, timestamp)

    @classmethod
    def user_task_facts(cls, message: Message) -> Iterable[object]:
        return message.task.user_task_facts(message)
