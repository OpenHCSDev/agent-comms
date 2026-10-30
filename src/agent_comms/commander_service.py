"""The commander service's sender and live request holders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .channel_targets import CommandersTag
from .channels import ChannelMemberIndex
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
    def members(cls, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        """The registered commander and every #commanders member, each once."""
        return tuple(dict.fromkeys((
            *snapshot.commander_project.commanders(snapshot),
            *ChannelMemberIndex(snapshot.threads.values())[CommandersTag.declared_name],
        )))

    @classmethod
    def actors(cls, snapshot: RegistrySnapshot) -> frozenset[str]:
        """Every name a commander or one of its forks publishes under: any commander's recorded
        action disposes of a line on the one shared board."""
        return frozenset(
            name for commander in cls.members(snapshot)
            for actor in (commander, *cls.forks(snapshot, commander.name))
            for name in snapshot.names_for(actor.name))

    @classmethod
    def recipients(cls, snapshot: RegistrySnapshot, commander: Thread) -> tuple[Thread, ...]:
        """The board's commander, then every other live commander that works its own turns:
        one board read, one packet each. A fork works inside its parent's turn and gets none."""
        return tuple(dict.fromkeys((commander, *(
            owner for member in cls.members(snapshot) if member.process_alive
            for owner in member.execution.turn_owners(member)
        ))))

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
