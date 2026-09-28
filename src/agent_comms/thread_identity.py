"""A7: thread provenance, process ownership and turn identity are distinct facts.

Names are resolved by the registry before constructing an identity. A restart
does not create a new historical thread; a new turn does not change its owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .declarations import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class ThreadIncarnation:
    name: str
    created_at: float

    def current(self, snapshot: RegistrySnapshot) -> bool:
        thread = snapshot.threads.get(self.name)
        return (
            thread is None
            if self.created_at == -1.0
            else thread is not None and thread.incarnation == self
        )


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    incarnation: ThreadIncarnation
    generation: int


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    owner: OwnerIdentity
    generation: int

    @property
    def incarnation(self) -> ThreadIncarnation:
        return self.owner.incarnation


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

    def advance(self, name: str) -> int:
        self.counter += 1
        self.generations[name] = self.counter
        return self.counter

    def rename(self, old: str, new: str) -> None:
        self.generations[new] = self.generations.pop(old)
