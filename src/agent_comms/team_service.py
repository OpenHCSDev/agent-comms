"""The teammate service's sender and live request holders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

from .threads import Thread

if TYPE_CHECKING:
    from .goal_management import Goals
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .todos import OwnerRequest


class HeartbeatDuty(ABC):
    """Whether a pass acts on what it observes. The board frame is written either way: it is a
    view Tristan reads, never a service someone must be owed (2026-11-02: with no open request
    the heartbeat wrote no frame for 13 hours, and every HUD read said "frame missing")."""

    @abstractmethod
    def actionable[Line](self, lines: tuple[Line, ...]) -> tuple[Line, ...]:
        """The observed lines Core enforces this pass."""

    @abstractmethod
    def publish(self, send: Callable[[], object]) -> None:
        """Send one teammate's packet, or keep it recorded and unsent."""


class OwedHeartbeat(HeartbeatDuty):
    """A live teammate holds an open owner request: enforce and publish."""

    def actionable[Line](self, lines: tuple[Line, ...]) -> tuple[Line, ...]:
        return lines

    def publish(self, send: Callable[[], object]) -> None:
        send()


class UnownedHeartbeat(HeartbeatDuty):
    """Nobody is owed service: a team that stood down gets no reminder, enforcement or packet."""

    def actionable[Line](self, lines: tuple[Line, ...]) -> tuple[Line, ...]:
        return ()

    def publish(self, send: Callable[[], object]) -> None:
        return


class TeamService:
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
        """The teammate's forks Core registered, heard from or not."""
        return tuple(
            fork for thread in snapshot.children(commander)
            for fork in thread.execution.forks(thread)
        )

    @classmethod
    def actors(cls, snapshot: RegistrySnapshot) -> frozenset[str]:
        """Current and retained names of executable collaborators, from the namespace owner.

        Walk its declared members once instead of rescanning aliases for every peer.
        """
        return frozenset(
            name for name, thread in snapshot.named_members()
            for _ in thread.role.executor(thread))

    @classmethod
    def recipients(cls, snapshot: RegistrySnapshot, lead: Thread) -> tuple[Thread, ...]:
        """The board's lead, then every other live teammate: one board read, one packet each."""
        return tuple(dict.fromkeys((lead, *cls.live(snapshot.commander_project.commanders(snapshot)))))

    @classmethod
    def served(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """Every teammate a pass sends a packet to: each board's recipients, once."""
        snapshot = registry.snapshot()
        return tuple(dict.fromkeys(
            recipient for lead in cls.boards(registry, goals)
            for recipient in cls.recipients(snapshot, lead)))

    @classmethod
    def boards(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """The registered lead's board, the one the heartbeat reads and writes every pass: a
        lead's closed session must not blind the teammates still running.

        Command moves with the registry, never with an old request: a thread that stood down
        keeps its parked orders and requests, and gets no board, heartbeat or vibe check."""
        return registry.snapshot().commander_project.leads(registry.snapshot())

    @classmethod
    def duty(cls, registry: Registration, goals: Goals) -> HeartbeatDuty:
        """Owed while any live teammate holds an open owner request; the pass acts only then."""
        snapshot = registry.snapshot()
        owed = any(goals.todos.open_requests(thread.acting_identity, limit=1, descending=False)
                   for thread in cls.live(snapshot.commander_project.commanders(snapshot)))
        return OwedHeartbeat() if owed else UnownedHeartbeat()
