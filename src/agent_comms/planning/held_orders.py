"""Shared held work, split once by whether each holder is still registered."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from textwrap import shorten

from agent_comms.planning.orders import Todo, TodoStore
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread


@dataclass(frozen=True)
class LiveHold:
    """A held order and the registered thread its recorded holder still is."""

    todo: Todo
    holder: Thread

    def recalled(self) -> str:
        """The holder, problem, scope and last numbered edit, read from the stored order."""
        order = self.todo.order.checked()
        return (f"{self.holder.name} {self.todo.id} {order.scope.display_label()}: "
                + shorten(" ".join(order.edits[-1:]), 160, placeholder="..."))


@dataclass(frozen=True)
class HeldOrders:
    """The shared board's held orders split once against one registry cut: live holds, whose
    recorded holder is still registered, and orphaned holds, whose holder was deleted or
    registered again since the order. Every reader takes its side of this split."""

    live: tuple[LiveHold, ...]
    orphaned: tuple[Todo, ...]

    @classmethod
    def of(cls, store: TodoStore, snapshot: RegistrySnapshot) -> HeldOrders:
        """Held work in ``store`` as this one registry cut registers its holders."""
        return cls.split(store.held_orders(), snapshot)

    @classmethod
    def from_open_orders(
        cls, todos: Sequence[Todo], snapshot: RegistrySnapshot,
    ) -> HeldOrders:
        """Derive held work from an already captured open-order board."""
        return cls.split(cls.assigned(todos), snapshot)

    @staticmethod
    def assigned(todos: Sequence[Todo]) -> tuple[Todo, ...]:
        """Assigned rows in their open-board order, whether or not the holder remains live."""
        return tuple(todo for todo in todos if todo.assignment is not None)

    @classmethod
    def split(cls, todos: Sequence[Todo], snapshot: RegistrySnapshot) -> HeldOrders:
        holds = tuple((todo, todo.held().holder_in(snapshot)) for todo in todos)
        return HeldOrders(
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
