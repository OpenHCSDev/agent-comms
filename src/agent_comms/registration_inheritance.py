"""Declaration-owned inheritance of omitted registration metadata."""

from abc import ABC, abstractmethod
from typing import TypeVar

T = TypeVar("T")


class RegistrationInheritance(ABC):
    @classmethod
    def choose(cls, incoming: T, previous: T) -> T:
        return previous if cls.inherits(incoming) else incoming

    @staticmethod
    @abstractmethod
    def inherits(incoming: object) -> bool: ...


class InheritMissing(RegistrationInheritance):
    @staticmethod
    def inherits(incoming: object) -> bool:
        return incoming is None


class InheritEmpty(RegistrationInheritance):
    @staticmethod
    def inherits(incoming: object) -> bool:
        return not incoming


class InheritPrevious(RegistrationInheritance):
    @staticmethod
    def inherits(incoming: object) -> bool:
        return True
