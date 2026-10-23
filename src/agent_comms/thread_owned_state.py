"""Stores whose rows belong to one thread declaration and end with it."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from .store_files import StoreOwner
from .errors import RelationViolationError

if TYPE_CHECKING:
    from .threads import Thread


class ThreadOwnedState(StoreOwner):
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
    def consulted_owners(cls) -> frozenset[type[ThreadOwnedState]]:
        """The declared storage owners this held store supplies through inheritance."""
        return frozenset(
            owner for owner in ThreadOwnedState.members_with(ThreadOwnedState)
            if owner in cls.__mro__
        )

    @classmethod
    def require_complete(cls, held: Iterable[ThreadOwnedState]) -> None:
        """Fail before any removal when a declared owner is not consulted."""
        consulted = frozenset(owner for store in held for owner in store.consulted_owners())
        missing = sorted(
            owner.__qualname__
            for owner in cls.members_with(cls)
            if owner not in consulted
        )
        if missing:
            raise RelationViolationError(
                f"Thread deletion does not consult declared owners: {', '.join(missing)}"
            )
