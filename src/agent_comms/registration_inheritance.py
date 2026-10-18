"""Declaration-owned inheritance of omitted registration metadata."""

from abc import ABC, abstractmethod

from .pi_vocabulary import ThinkingLevel


class RegistrationInheritance[T](ABC):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return previous if cls.inherits(incoming) else incoming

    @classmethod
    @abstractmethod
    def inherits(cls, incoming: T) -> bool: ...


class InheritMissing[T](RegistrationInheritance[T]):
    @classmethod
    def inherits(cls, incoming: T) -> bool:
        return incoming is None


class InheritEmpty[T](RegistrationInheritance[T]):
    @classmethod
    def inherits(cls, incoming: T) -> bool:
        return not incoming


class InheritPrevious[T](RegistrationInheritance[T]):
    @classmethod
    def inherits(cls, incoming: T) -> bool:
        return True


class InheritThinkingLevel(RegistrationInheritance[type[ThinkingLevel]]):
    @classmethod
    def choose(cls, incoming: type[ThinkingLevel], previous: type[ThinkingLevel]) -> type[ThinkingLevel]:
        return incoming.applied(previous)
