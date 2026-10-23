"""The declared capability of code that produces derived projection rows."""

from __future__ import annotations

from typing import ClassVar


class ProjectionSource:
    """A class whose code decides projection row content.

    Its source, and every subclass's source, belongs to the digest that tells a
    stored projection it is still fresh. A class reached while rows are produced
    that does not inherit this capability is a hole in that digest.
    """

    __slots__ = ()
    _sources: ClassVar[list[type[ProjectionSource]]] = []

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        ProjectionSource._sources.append(cls)

    @staticmethod
    def sources() -> tuple[type[ProjectionSource], ...]:
        return tuple(ProjectionSource._sources)
