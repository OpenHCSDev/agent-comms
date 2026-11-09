"""The teammate service's sender and live request holders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from agent_comms.errors import NobodyOwedError
from agent_comms.planning.orders import OwnerRequest
from agent_comms.registry.threads import Thread

if TYPE_CHECKING:
    from agent_comms.planning.goal_management import Goals
    from agent_comms.registry.registration import Registration
    from agent_comms.registry.registry_document import RegistrySnapshot


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
    def addressable(cls, snapshot: RegistrySnapshot,
                    threads: Iterable[Thread]) -> tuple[Thread, ...]:
        """The independent colleagues among these that Core may address: an archived or
        deleting thread is not (ThreadStatus.publish_heartbeat owns that)."""
        return tuple(thread for thread in snapshot.team_project.participants(threads)
                     if snapshot.status(thread.name).publish_heartbeat(lambda: True, False))

    @classmethod
    def teammates(cls, snapshot: RegistrySnapshot, goals: Goals) -> tuple[Thread, ...]:
        """The team: every running colleague, and every colleague one of Tristan's open
        requests waits on, running or not. There is no lead; the board, its warnings and its
        pauses belong to each of them alike. A stopped session holding his ask keeps its owed
        answers and frame on the board, archived or not (publish_heartbeat still declines to
        message an unaddressable one); a finished session does not, so the board scales with
        the team, never with every thread the registry has held."""
        owed = (thread for owner in goals.todos.requesters()
                for thread in snapshot.threads_for(owner.require_recorded()))
        return tuple(dict.fromkeys(
            (*cls.running(snapshot), *snapshot.team_project.participants(owed))))

    @classmethod
    def forks(cls, snapshot: RegistrySnapshot, teammates: Iterable[Thread]) -> tuple[Thread, ...]:
        """These teammates' forks Core registered, heard from or not."""
        return tuple(dict.fromkeys(
            fork for teammate in teammates
            for thread in snapshot.children(teammate.name)
            for fork in thread.forks()
        ))

    @classmethod
    def actors(cls, snapshot: RegistrySnapshot) -> frozenset[str]:
        """Current and retained names of executable collaborators, from the namespace owner.

        Walk its declared members once instead of rescanning aliases for every peer.
        """
        return frozenset(
            name for name, thread in snapshot.named_members()
            for _ in thread.role.executor(thread))

    @classmethod
    def running(cls, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        """The teammates that can act now. A warning that owes an answer within minutes goes
        only to them: a stopped session cannot be ignoring what it never read."""
        return cls.live(cls.addressable(snapshot, snapshot.threads.values()))

    @classmethod
    def longest_owed(cls, registry: Registration, goals: Goals) -> Thread:
        """The live teammate holding the oldest open owner request. An order Core starts itself
        must name exactly one open request. Only an owed pass acts, but that teammate can stop
        between the duty read and the act: then this refuses as not applied (NobodyOwedError)
        instead of failing the pass."""
        owed = tuple((request.created_at, teammate)
                     for teammate in cls.running(registry.snapshot())
                     for request in cls.requested_by(teammate, goals))
        if not owed:
            raise NobodyOwedError()
        _, teammate = min(owed, key=lambda pair: pair[0])
        return teammate

    @classmethod
    def served(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """Every teammate a pass reads one team board for and sends its packet to."""
        return cls.teammates(registry.snapshot(), goals)

    @classmethod
    def duty(cls, registry: Registration, goals: Goals) -> HeartbeatDuty:
        """Owed while any live teammate holds an open owner request; the pass acts only then."""
        owed = any(cls.requested_by(thread, goals) for thread in cls.running(registry.snapshot()))
        return OwedHeartbeat() if owed else UnownedHeartbeat()
