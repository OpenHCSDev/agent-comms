"""A7: thread provenance, process ownership and turn identity are distinct facts.

Names are resolved by the registry before constructing an identity. A restart
does not create a new historical thread; a new turn does not change its owner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from .child_process import ProcessIdentity
from .errors import RelationViolationError
from .thread_execution import ThreadExecution, NativeThreadExecution

if TYPE_CHECKING:
    from .registry_provenance import RegistryProvenance


@dataclass(frozen=True, slots=True)
class ThreadIncarnation:
    name: str
    created_at: float

    # The persisted birth of a deleted participant: its declaration and real
    # birth are gone, its name stays on the bus. See DeletedThread.
    DELETED_BIRTH: ClassVar[float] = -1.0

    @classmethod
    def deleted(cls, name: str) -> ThreadIncarnation:
        return cls(name, cls.DELETED_BIRTH)

    @property
    def names_deleted_thread(self) -> bool:
        return self.created_at == self.DELETED_BIRTH

    def __post_init__(self) -> None:
        from .field_codec import FieldCodec

        if not FieldCodec.decode(str, self.name):
            raise ValueError("Thread incarnation requires a recorded name")
        if not math.isfinite(FieldCodec.decode(float, self.created_at)):
            raise ValueError("Thread incarnation requires a finite birth")

    def require_recorded(self) -> None:
        if self.created_at <= 0:
            raise ValueError("Thread incarnation requires a positive recorded birth")

    def resolved(self, snapshot: RegistryProvenance) -> ThreadIncarnation:
        """Follow retained rename aliases only for this exact historical owner."""
        if self.names_deleted_thread or not self.current(snapshot):
            return self
        return snapshot.threads[snapshot.canonical_name(self.name)].incarnation

    def current(self, snapshot: RegistryProvenance) -> bool:
        thread = snapshot.threads.get(snapshot.canonical_name(self.name))
        return (
            thread is None
            if self.names_deleted_thread
            else thread is not None and thread.created_at == self.created_at
        )

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
        return (canonical, *(type(self)(alias, canonical.created_at)
                             for alias, name in snapshot.aliases.items()
                             if name == canonical.name))


@dataclass(frozen=True, slots=True)
class TurnId:
    value: str

    @classmethod
    def for_registration(cls, value: str) -> TurnId:
        from .field_codec import FieldCodec

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

    def resolved(self, snapshot: RegistrySnapshot) -> TurnIdentity:
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
        from .field_codec import FieldCodec

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


@dataclass(frozen=True, slots=True)
class ThreadPublicationIdentity:
    """Registration facts protected during canonical compaction publication.

    Metadata, channel scope and goal changes are not session identity changes.
    Owner/admission generations independently fence execution and are not this
    identity: changing a saved session alone does not allocate a new executor.
    """

    incarnation: ThreadIncarnation
    process: ProcessIdentity | None
    role: ThreadRole
    session_file: str | None
    worktree: str
    execution: type[ThreadExecution] = field(default=NativeThreadExecution,
                                            metadata={"wire_omit_default": True})
