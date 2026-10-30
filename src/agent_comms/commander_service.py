"""The commander service's sender and live request holders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .threads import Thread

if TYPE_CHECKING:
    from .goal_management import Goals
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .todos import OwnerRequest


class CommanderService:
    @classmethod
    def requested_by(cls, thread: Thread, goals: Goals) -> tuple[OwnerRequest, ...]:
        """The oldest open request makes a live holder owed service, even before its first beat."""
        return (goals.todos.open_requests(thread.incarnation, limit=1, descending=False)
                if thread.process_alive else ())

    @classmethod
    def forks(cls, snapshot: RegistrySnapshot, commander: str) -> tuple[Thread, ...]:
        """The commander's forks Core registered, heard from or not."""
        return tuple(
            fork for thread in snapshot.children(commander)
            for fork in thread.execution.commander_forks(thread)
        )

    @classmethod
    def actors(cls, snapshot: RegistrySnapshot, commander: str) -> frozenset[str]:
        """Every name the commander or one of its forks publishes under."""
        return frozenset(name for actor in (commander, *(fork.name for fork in cls.forks(snapshot, commander)))
                         for name in snapshot.names_for(actor))

    @classmethod
    def commanders(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """The threads the registry names commander that are live and owed service.

        Command moves with the registry, never with an old request: a thread that stood down
        keeps its parked orders and requests, and gets no heartbeat or vibe check for them."""
        snapshot = registry.snapshot()
        return tuple(
            thread
            for thread in snapshot.commander_project.commanders(snapshot)
            if cls.requested_by(thread, goals)
        )
