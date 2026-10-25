"""Writer-owned declarations of the projection tables their code produces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class ProducedRow(ABC):
    """Rows rebuilt when the source of a class producing them changes."""

    @classmethod
    @abstractmethod
    def producer_digest(cls) -> str: ...

class ProjectionOutputs:
    """Output declarations can be shared without registering their capability as a writer."""

    __slots__ = ()
    writes: ClassVar[tuple[type[ProducedRow], ...]]

    @classmethod
    def shares_outputs_with(cls, writer: type[ProjectionOutputs]) -> bool:
        return True

    @classmethod
    def writer_declarations(cls) -> tuple[type[ProjectionOutputs], ...]:
        """Derive output owners from the class hierarchy, including shared capabilities."""
        return tuple(parent for parent in cls.__mro__ if "writes" in vars(parent))


class ProjectionSource(ProjectionOutputs):
    """Actual writers register the output declarations inherited by their members.

    ClassFact defers table references until imports finish. A table may inherit
    another writer's fields without becoming a second writer of its outputs.
    """

    __slots__ = ()
    _declarations: ClassVar[dict[type[ProjectionOutputs], set[type[ProjectionSource]]]] = {}

    @classmethod
    def shares_outputs_with(cls, writer: type[ProjectionOutputs]) -> bool:
        return cls is writer

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for parent in cls.writer_declarations():
            ProjectionSource._declarations.setdefault(parent, set()).add(cls)

    @staticmethod
    def writers_for(table: type[ProducedRow]) -> set[type[ProjectionSource]]:
        """Reverse the writers' declarations, without discovering unrelated modules."""
        return {
            writer
            for owner, writers in tuple(ProjectionSource._declarations.items())
            for writer in writers
            if table in owner.writes
        }
