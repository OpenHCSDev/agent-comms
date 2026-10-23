"""Writer-owned declarations of the projection tables their code produces."""

from __future__ import annotations

from typing import ClassVar

from .class_fact import ClassFact


class ProjectionSource:
    """A writer declares ``writes`` as table classes, inherited by its members.

    ClassFact defers the table references until imports finish: a value such as
    MessageReference can produce a table that itself inherits MessageReference.
    Only explicit output declarations register writers; importing another subclass
    without such a declaration contributes nothing to any projection.
    """

    __slots__ = ()
    _declarations: ClassVar[dict[ClassFact[ProjectionSource, tuple[type, ...]], set[type]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for parent in cls.__mro__:
            if "writes" in vars(parent):
                declaration = vars(parent)["writes"]
                ProjectionSource._declarations.setdefault(declaration, set()).add(cls)

    @staticmethod
    def writers_for(table: type) -> set[type]:
        """Reverse the writers' declarations, without discovering unrelated modules."""
        return {
            writer
            for declaration, writers in ProjectionSource._declarations.items()
            for writer in writers
            if table in declaration.__get__(None, writer)
        }
