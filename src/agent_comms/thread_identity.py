"""A7: thread provenance, process ownership and turn identity are distinct facts.

Names are resolved by the registry before constructing an identity. A restart
does not create a new historical thread; a new turn does not change its owner.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Callable, ClassVar, TypeVar

from .channel_targets import Tag
from .cli_options import JsonFlagRecord
from .errors import RelationViolationError
from .field_codec import FieldCodec, FieldRepresentation, WireValue

if TYPE_CHECKING:
    from .registry_provenance import RegistryProvenance

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ThreadIncarnation(JsonFlagRecord, WireValue, ABC):
    value_flag: ClassVar[str] = "--thread-incarnation"
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
    def provenance_type(self):
        """The archive declaration carrying this recorded birth state."""

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
        return thread is not None and thread.incarnation == self.renamed(thread.name)

    def recorded_or(
        self, make: Callable[[RecordedThreadIncarnation], T], otherwise: Callable[[], T]
    ) -> T:
        return otherwise()

    def selects_lookup(self, lookup: str) -> bool:
        return False

    def same_publication_birth(self, other: ThreadIncarnation) -> bool:
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


@dataclass(frozen=True)
class RecordedThreadIncarnation(ThreadIncarnation):
    """Only a positive recorded birth can select a durable lookup."""

    created_at: float

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

    def same_publication_birth(self, other: ThreadIncarnation) -> bool:
        return self.renamed(other.name) == other

    @property
    def birth_description(self) -> str:
        return f"created {self.created_at}"

    def provenance_type(self):
        return ThreadProvenance

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


@dataclass(frozen=True)
class DeletedThreadIncarnation(ThreadIncarnation):
    """A deleted participant retains only its name, never a lookup capability."""

    WIRE_BIRTH: ClassVar[float] = -1.0

    @property
    def wire_birth(self) -> float:
        return self.WIRE_BIRTH

    def current(self, snapshot: RegistryProvenance) -> bool:
        return snapshot.canonical_name(self.name) not in snapshot.threads

    def resolved(self, snapshot: RegistryProvenance) -> DeletedThreadIncarnation:
        return self

    def require_recorded(self) -> RecordedThreadIncarnation:
        raise ValueError("Thread incarnation requires a positive recorded birth")

    @property
    def birth_description(self) -> str:
        return "deleted declaration"

    def provenance_type(self):
        return DeletedThreadProvenance

    def renamed(self, name: str) -> DeletedThreadIncarnation:
        return DeletedThreadIncarnation(name)


@dataclass(frozen=True)
class UnrecordedThreadIncarnation(ThreadIncarnation):
    """Historical zero-birth evidence cannot select a live recipient."""

    WIRE_BIRTH: ClassVar[float] = 0.0

    @property
    def wire_birth(self) -> float:
        return self.WIRE_BIRTH

    def require_recorded(self) -> RecordedThreadIncarnation:
        raise ValueError("Thread incarnation requires a positive recorded birth")

    @property
    def birth_description(self) -> str:
        return "birth unrecorded"

    def provenance_type(self):
        return UnrecordedThreadProvenance

    def renamed(self, name: str) -> UnrecordedThreadIncarnation:
        return UnrecordedThreadIncarnation(name)


@dataclass(frozen=True, kw_only=True)
class ThreadProvenance:
    """Recorded thread provenance, without process, turn or admission authority."""

    name: str
    tags: frozenset[str]
    worktree: str
    created_at: float = field(metadata={"wire_required": True})
    session_file: str | None = None
    title: str | None = None

    @property
    def display_title(self) -> str:
        return self.name if self.title is None else self.title

    def make_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.name, self.created_at)

    @property
    def provenance_type(self) -> type[ThreadProvenance]:
        return ThreadProvenance

    def __post_init__(self) -> None:
        if self.incarnation.wire_birth != self.created_at:
            raise ValueError("Thread provenance birth differs from its declared state")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not self.name or not set(self.name) <= allowed:
            raise ValueError(f"Thread name {self.name!r} must be alphanumeric with hyphens/underscores.")
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")
        for tag in self.tags:
            Tag(tag)

    @staticmethod
    def capture(thread: ThreadProvenance) -> ThreadProvenance:
        """Freeze only determining provenance from the original typed declaration."""
        return thread.provenance_type(
            **{
                item.name: getattr(thread, item.name)
                for item in fields(ThreadProvenance)
                if item.init
            }
        )

    @cached_property
    def incarnation(self) -> ThreadIncarnation:
        """The declaration's birth member, created once and shared by its readers."""
        return self.make_incarnation()


@dataclass(frozen=True, slots=True, kw_only=True)
class UnrecordedThreadProvenance(ThreadProvenance):
    def make_incarnation(self) -> ThreadIncarnation:
        return UnrecordedThreadIncarnation(self.name)

    @property
    def provenance_type(self) -> type[ThreadProvenance]:
        return UnrecordedThreadProvenance


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletedThreadProvenance(ThreadProvenance):
    def make_incarnation(self) -> ThreadIncarnation:
        return DeletedThreadIncarnation(self.name)

    @property
    def provenance_type(self) -> type[ThreadProvenance]:
        return DeletedThreadProvenance


@dataclass(frozen=True, slots=True)
class ThreadIncarnationRecord:
    """The single exact external name/birth record; members own its meaning."""

    name: str
    created_at: float

    def incarnation(self) -> ThreadIncarnation:
        if self.created_at == DeletedThreadIncarnation.WIRE_BIRTH:
            return DeletedThreadIncarnation(self.name)
        if self.created_at == UnrecordedThreadIncarnation.WIRE_BIRTH:
            return UnrecordedThreadIncarnation(self.name)
        return RecordedThreadIncarnation(self.name, self.created_at)

    @classmethod
    def decode(cls, name: str, birth: float) -> ThreadIncarnation:
        return FieldCodec.decode(cls, {"name": name, "created_at": birth}).incarnation()


class ThreadProvenanceRecord(FieldRepresentation):
    """Decode an archived registry row's recorded birth state exactly once."""

    @classmethod
    def encode(cls, value: ThreadProvenance) -> object:
        return FieldCodec.encode(value)

    @classmethod
    def schema(cls) -> dict:
        return FieldCodec.record_schema(ThreadProvenance)

    @classmethod
    def decode(cls, value: object) -> ThreadProvenance:
        if not isinstance(value, dict):
            raise TypeError("Recorded thread provenance must be an object")
        identity = ThreadIncarnationRecord.decode(value["name"], value["created_at"])
        return FieldCodec.decode(identity.provenance_type(), value)


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


@dataclass(frozen=True, slots=True)
class AdmissionIdentity:
    """An allocation from RegistryDocument.admissions, never an owner lease."""

    incarnation: ThreadIncarnation
    admission_generation: int

    def includes(self, previous: AdmissionIdentity) -> bool:
        """Order only within this exact registry admission allocation domain."""
        return self.incarnation == previous.incarnation and self.admission_generation >= previous.admission_generation


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    incarnation: ThreadIncarnation
    generation: int

    def resolved(self, snapshot: RegistryProvenance) -> TurnIdentity:
        """Resolve the original incarnation; retain this turn allocation."""
        return replace(self, incarnation=self.incarnation.resolved(snapshot))


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


class ThreadRole(StrEnum):
    AGENT = "agent"
    USER = "user"

    @property
    def executable(self) -> bool:
        return self is self.AGENT

    def require_executable(self) -> None:
        if not self.executable:
            raise RelationViolationError("thread role does not execute owner turns")

    def require_user(self) -> None:
        if self is not self.USER:
            raise RelationViolationError("A user correction requires the original human sender")


