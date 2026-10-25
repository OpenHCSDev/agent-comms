"""A commander's held orders, split once by whether each holder is still registered."""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import shorten
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_comms.comms import Comms
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.threads import Thread
    from agent_comms.todos import Todo, TodoStore


@dataclass(frozen=True)
class LiveHold:
    """A held order and the registered thread its recorded holder still is."""

    todo: Todo
    holder: Thread

    def recalled(self) -> str:
        """The holder, problem, scope and last numbered edit, read from the stored order."""
        order = self.todo.order.checked()
        return (f"{self.holder.name} {self.todo.id} {order.scope.declared_name}: "
                + shorten(" ".join(order.edits[-1:]), 160, placeholder="..."))


@dataclass(frozen=True)
class HeldOrders:
    """A commander's held orders split once against one registry cut: live holds, whose
    recorded holder is still registered, and orphaned holds, whose holder was deleted or
    registered again since the order. Every reader takes its side of this split."""

    live: tuple[LiveHold, ...]
    orphaned: tuple[Todo, ...]

    @classmethod
    def read(cls, comms: Comms, commander: str) -> Self:
        """The commander's held orders captured once; filtering never reopens the store."""
        return cls.of(comms.goals.todos, comms.registry.snapshot(), commander)

    @classmethod
    def of(cls, store: TodoStore, snapshot: RegistrySnapshot, commander: str) -> Self:
        """The held orders in ``store`` for ``commander`` as ``snapshot`` registers them."""
        return cls.split(store.held_for_commander(snapshot.require(commander).incarnation),
                         snapshot)

    @classmethod
    def split(cls, todos: Sequence[Todo], snapshot: RegistrySnapshot) -> Self:
        holds = tuple((todo, todo.held().holder_in(snapshot)) for todo in todos)
        return cls(
            tuple(LiveHold(todo, thread) for todo, threads in holds for thread in threads),
            tuple(todo for todo, threads in holds if not threads),
        )

    @property
    def todos(self) -> tuple[Todo, ...]:
        return tuple(hold.todo for hold in self.live)

    @property
    def holders(self) -> dict[ThreadIncarnation, list[Todo]]:
        """Live holds by their holder's current registration."""
        held: dict[ThreadIncarnation, list[Todo]] = {}
        for hold in self.live:
            held.setdefault(hold.holder.incarnation, []).append(hold.todo)
        return held
