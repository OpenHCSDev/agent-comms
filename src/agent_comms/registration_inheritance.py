"""Declaration-owned inheritance of omitted registration metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .pi_vocabulary import ThinkingChoice


class RegistrationInheritance[T](ABC):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return cls.apply(incoming, previous)

    @classmethod
    @abstractmethod
    def apply(cls, incoming: T, previous: T) -> T: ...


class PredicateInheritance[T](RegistrationInheritance[T]):
    @classmethod
    def apply(cls, incoming: T, previous: T) -> T:
        return previous if cls.inherits(incoming) else incoming

    @classmethod
    @abstractmethod
    def inherits(cls, incoming: object) -> bool: ...


class InheritMissing[T](PredicateInheritance[T]):
    @classmethod
    def inherits(cls, incoming: object) -> bool:
        return incoming is None


class InheritEmpty[T](PredicateInheritance[T]):
    @classmethod
    def inherits(cls, incoming: object) -> bool:
        return not incoming


class InheritPrevious[T](PredicateInheritance[T]):
    @classmethod
    def inherits(cls, incoming: object) -> bool:
        return True


class InheritThinkingLevel(RegistrationInheritance[type[ThinkingChoice]]):
    @classmethod
    def apply(
        cls,
        incoming: type[ThinkingChoice],
        previous: type[ThinkingChoice],
    ) -> type[ThinkingChoice]:
        return incoming.applied(previous)
