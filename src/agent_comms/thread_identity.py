"""A7: thread provenance, process ownership and turn identity are distinct facts.

Names are resolved by the registry before constructing an identity. A restart
does not create a new historical thread; a new turn does not change its owner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .child_process import ProcessIdentity
from .errors import RelationViolationError

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class ThreadIncarnation:
    name: str
    created_at: float

    def __post_init__(self) -> None:
        from .field_codec import FieldCodec

        if not FieldCodec.decode(str, self.name):
            raise ValueError("Thread incarnation requires a recorded name")
        if not math.isfinite(FieldCodec.decode(float, self.created_at)):
            raise ValueError("Thread incarnation requires a finite birth")

    def resolved(self, snapshot: RegistrySnapshot) -> ThreadIncarnation:
        """Follow retained rename aliases only for this exact historical owner."""
        if self.created_at == -1.0 or not self.current(snapshot):
            return self
        return snapshot.threads[snapshot.aliases.get(self.name, self.name)].incarnation

    def current(self, snapshot: RegistrySnapshot) -> bool:
        thread = snapshot.threads.get(snapshot.aliases.get(self.name, self.name))
        return (
            thread is None
            if self.created_at == -1.0
            else thread is not None and thread.created_at == self.created_at
        )

    def matches_recorded_name(self, name: str, snapshot: RegistrySnapshot) -> bool:
        """Relate a partial stored name to this original current incarnation.

        The registry owns aliases and birth. A stored name does not supply or
        manufacture either fact, including after a name is reused.
        """
        thread = snapshot.threads.get(snapshot.aliases.get(name, name))
        return thread is not None and self.resolved(snapshot) == thread.incarnation


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


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    incarnation: ThreadIncarnation
    generation: int


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
