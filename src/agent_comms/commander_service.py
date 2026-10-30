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
    def live(cls, threads: tuple[Thread, ...]) -> tuple[Thread, ...]:
        """The threads with a live owner process: the one meaning of live the heartbeat serves."""
        return tuple(thread for thread in threads if thread.process_alive)

    @classmethod
    def requested_by(cls, thread: Thread, goals: Goals) -> tuple[OwnerRequest, ...]:
        """The oldest open request makes a live holder owed service, even before its first beat."""
        return tuple(request for live in cls.live((thread,))
                     for request in goals.todos.open_requests(live.acting_identity, limit=1, descending=False))

    @classmethod
    def forks(cls, snapshot: RegistrySnapshot, commander: str) -> tuple[Thread, ...]:
        """The commander's forks Core registered, heard from or not."""
        return tuple(
            fork for thread in snapshot.children(commander)
            for fork in thread.execution.commander_forks(thread)
        )

    @classmethod
    def actors(cls, snapshot: RegistrySnapshot) -> frozenset[str]:
        """Every name a commander or one of its forks publishes under: any commander's recorded
        action disposes of a line on the one shared board."""
        return frozenset(
            name for commander in snapshot.commander_project.commanders(snapshot)
            for actor in (commander, *cls.forks(snapshot, commander.name))
            for name in snapshot.names_for(actor.name))

    @classmethod
    def recipients(cls, snapshot: RegistrySnapshot, lead: Thread) -> tuple[Thread, ...]:
        """The board's lead, then every other live commander: one board read, one packet each."""
        return tuple(dict.fromkeys((lead, *cls.live(snapshot.commander_project.commanders(snapshot)))))

    @classmethod
    def served(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """Every commander a pass sends a packet to: each board's recipients, once."""
        snapshot = registry.snapshot()
        return tuple(dict.fromkeys(
            recipient for lead in cls.boards(registry, goals)
            for recipient in cls.recipients(snapshot, lead)))

    @classmethod
    def boards(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """The registered lead's board, the one the heartbeat reads, while any live commander is
        owed service: a lead's closed session must not blind the commanders still running.

        Command moves with the registry, never with an old request: a thread that stood down
        keeps its parked orders and requests, and gets no heartbeat or vibe check for them."""
        snapshot = registry.snapshot()
        project = snapshot.commander_project
        commanders = project.commanders(snapshot)
        if not cls.live(commanders) or not any(
                goals.todos.open_requests(thread.acting_identity, limit=1, descending=False)
                for thread in commanders):
            return ()
        return project.leads(snapshot)
