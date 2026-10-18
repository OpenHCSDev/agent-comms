"""Declaration-owned inheritance of omitted registration metadata."""

from abc import ABC, abstractmethod
from typing import TypeVar

T = TypeVar("T")


class RegistrationInheritance(ABC):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return previous if cls.inherits(incoming) else incoming

    @classmethod
    @abstractmethod
    def inherits(cls, incoming) -> bool: ...


class InheritMissing(RegistrationInheritance):
    @classmethod
    def inherits(cls, incoming) -> bool:
        return incoming is None


class InheritEmpty(RegistrationInheritance):
    @classmethod
    def inherits(cls, incoming) -> bool:
        return not incoming


class InheritPrevious(RegistrationInheritance):
    @classmethod
    def inherits(cls, incoming) -> bool:
        return True


class InheritThinkingLevel(RegistrationInheritance):
    @classmethod
    def choose(cls, incoming, previous):
        return incoming.applied(previous)
