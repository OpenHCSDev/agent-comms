"""The nominal preparation contract loaded from a target distribution's metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod
from importlib.metadata import distribution
from inspect import isabstract
from typing import ClassVar


class RuntimePreparation(ABC):
    """A distribution prepares its own requirements before runtime publication."""

    entry_point_group: ClassVar[str] = "agent_comms.runtime_preparation"

    @abstractmethod
    def prepare(self) -> None: ...

    @classmethod
    def decode(cls, value: object) -> RuntimePreparation:
        """The entry point is foreign until its concrete nominal class is checked."""
        if not isinstance(value, type):
            raise TypeError("Runtime preparation entry point must load a class")
        if not issubclass(value, cls) or isabstract(value):
            raise TypeError(f"Runtime preparation must implement {cls.__qualname__}")
        return value()

    @classmethod
    def load(cls, package: str) -> RuntimePreparation:
        entries = tuple(distribution(package).entry_points.select(group=cls.entry_point_group))
        if len(entries) != 1:
            raise ValueError(
                f"{package} declares {len(entries)} {cls.entry_point_group} entry points; expected one"
            )
        return cls.decode(entries[0].load())
