"""A value owns a custom wire form; the record codec remains one mechanism."""

from abc import ABC, abstractmethod
from typing import Any, Self


class WireValue(ABC):
    __slots__ = ()

    @abstractmethod
    def to_wire(self) -> Any: ...

    @classmethod
    @abstractmethod
    def from_wire(cls, data: Any) -> Self: ...

    @classmethod
    def wire_schema(cls) -> dict[str, Any]:
        raise TypeError(f"{cls.__name__} has no declared JSON schema")
