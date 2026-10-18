"""Stores whose rows belong to one thread declaration and end with it."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .errors import RelationViolationError

if TYPE_CHECKING:
    from .threads import Thread


class ThreadOwnedState(DeclaredFamily):
    """A store holding state that exists only for its thread declarations.

    Declaring a store here makes its rows part of the thread's lifetime:
    permanent deletion consults every member. The bus and the records of
    uncertain inputs are not members; they outlive the declaration.
    """

    __slots__ = ()

    @abstractmethod
    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Remove every row these stopped declarations own; idempotent."""

    @classmethod
    def require_complete(cls, held: Iterable[ThreadOwnedState]) -> None:
        """Fail before any removal when a declared owner is not consulted."""
        held = tuple(held)
        missing = sorted(
            owner.__qualname__
            for owner in cls.members_with(cls)
            if not any(isinstance(store, owner) for store in held)
        )
        if missing:
            raise RelationViolationError(
                f"Thread deletion does not consult declared owners: {', '.join(missing)}"
            )
