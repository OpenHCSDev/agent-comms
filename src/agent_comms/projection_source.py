"""Writer-owned declarations of the projection tables their code produces."""

from __future__ import annotations

from typing import ClassVar

class ProjectionSource:
    """A writer declares ``writes`` as table classes, inherited by its members.

    ClassFact defers the table references until imports finish: a value such as
    MessageReference can produce a table that itself inherits MessageReference.
    Only explicit output declarations register writers; importing another subclass
    without such a declaration contributes nothing to any projection.
    """

    __slots__ = ()
    writes: ClassVar[tuple[type, ...]]
    _declarations: ClassVar[dict[type[ProjectionSource], set[type]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for parent in cls.writer_declarations():
            ProjectionSource._declarations.setdefault(parent, set()).add(cls)

    @classmethod
    def writer_declarations(cls) -> tuple[type[ProjectionSource], ...]:
        """Value-family members share their declared producing behavior."""
        return tuple(parent for parent in cls.__mro__ if "writes" in vars(parent))

    @staticmethod
    def writers_for(table: type) -> set[type]:
        """Reverse the writers' declarations, without discovering unrelated modules."""
        return {
            writer
            for owner, writers in ProjectionSource._declarations.items()
            for writer in writers
            if table in owner.writes
        }
