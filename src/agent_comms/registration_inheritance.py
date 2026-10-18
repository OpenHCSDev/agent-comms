"""Declaration-owned inheritance of omitted registration metadata."""

from abc import ABC, abstractmethod
from typing import TypeVar

from .pi_vocabulary import ThinkingLevel

T = TypeVar("T")


class RegistrationInheritance(ABC):
    @classmethod
    @abstractmethod
    def choose(cls, incoming: T, previous: T) -> T: ...


class PredicateInheritance(RegistrationInheritance):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return previous if cls.inherits(incoming) else incoming

    @classmethod
    @abstractmethod
    def inherits(cls, incoming: object) -> bool: ...


class InheritMissing(PredicateInheritance):
    @classmethod
    def inherits(cls, incoming: object) -> bool:
        return incoming is None


class InheritEmpty(PredicateInheritance):
    @classmethod
    def inherits(cls, incoming: object) -> bool:
        return not incoming


class InheritPrevious(PredicateInheritance):
    @classmethod
    def inherits(cls, incoming: object) -> bool:
        return True


class InheritThinkingLevel(RegistrationInheritance):
    @classmethod
    def choose(cls, incoming: type[ThinkingLevel], previous: type[ThinkingLevel]) -> type[ThinkingLevel]:
        return incoming.applied(previous)
