"""Stores whose rows belong to one thread declaration and end with it."""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from .errors import RelationViolationError

if TYPE_CHECKING:
    from .threads import Thread


class ThreadOwnedState(ABC):
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
    def declared_owners(cls) -> tuple[type[ThreadOwnedState], ...]:
        found: list[type[ThreadOwnedState]] = []
        pending = list(cls.__subclasses__())
        while pending:
            owner = pending.pop()
            pending.extend(owner.__subclasses__())
            # dataclass(slots=True) replaces its declaration; the module
            # binding is the declared owner, not the discarded original.
            if getattr(sys.modules[owner.__module__], owner.__qualname__, None) is owner:
                found.append(owner)
        return tuple(dict.fromkeys(found))

    @classmethod
    def require_complete(cls, held: Iterable[ThreadOwnedState]) -> None:
        """Fail before any removal when a declared owner is not consulted."""
        held = tuple(held)
        missing = sorted(
            owner.__qualname__
            for owner in cls.declared_owners()
            if not any(isinstance(store, owner) for store in held)
        )
        if missing:
            raise RelationViolationError(
                f"Thread deletion does not consult declared owners: {', '.join(missing)}"
            )
