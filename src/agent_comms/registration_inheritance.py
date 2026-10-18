"""Declaration-owned inheritance of omitted registration metadata."""

from abc import ABC, abstractmethod
from typing import TypeVar

T = TypeVar("T")


class RegistrationInheritance(ABC):
    @classmethod
    @abstractmethod
    def choose(cls, incoming: T, previous: T) -> T: ...


class InheritMissing(RegistrationInheritance):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return previous if incoming is None else incoming


class InheritEmpty(RegistrationInheritance):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return previous if not incoming else incoming


class InheritPrevious(RegistrationInheritance):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return previous


class InheritThinkingLevel(RegistrationInheritance):
    @classmethod
    def choose(cls, incoming, previous):
        return incoming.applied(previous)
