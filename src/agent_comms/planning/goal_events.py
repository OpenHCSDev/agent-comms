"""Goal observations published after their authoritative snapshot commits."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_comms.execution.agent_event import AgentEvent
from agent_comms.nominal.field_codec import NullMember
from agent_comms.planning.goal_presentation import GoalPublicationSnapshot, GoalSnapshot


class GoalChange(ABC):
    """A goal observation publishes its own change, including no change."""

    @staticmethod
    def observed(current: GoalSnapshot, previous: GoalPublicationSnapshot) -> GoalChange:
        return GoalChanged(current) if previous.changed_by(current) else GoalUnchanged()

    @abstractmethod
    async def publish(
        self, dispatch: Callable[[GoalChanged], Awaitable[None]],
        previous: GoalPublicationSnapshot,
    ) -> GoalPublicationSnapshot: ...


class GoalUnchanged(NullMember, GoalChange):
    async def publish(
        self, dispatch: Callable[[GoalChanged], Awaitable[None]],
        previous: GoalPublicationSnapshot,
    ) -> GoalPublicationSnapshot:
        return previous


@dataclass(frozen=True)
class GoalReadFailed(GoalChange):
    """A batch retains the original read failure for precisely its publication owner."""

    error: Exception

    async def publish(
        self, dispatch: Callable[[GoalChanged], Awaitable[None]],
        previous: GoalPublicationSnapshot,
    ) -> GoalPublicationSnapshot:
        raise self.error


@dataclass(frozen=True)
class GoalChanged(AgentEvent, GoalChange):
    """Current goal authority observed after durable registry/history publication.

    Cross-process notification uses the existing registry watcher/poll; the
    authoritative snapshot supplies this event, never a tool's spelling.
    """

    snapshot: GoalSnapshot

    async def publish(
        self, dispatch: Callable[[GoalChanged], Awaitable[None]],
        previous: GoalPublicationSnapshot,
    ) -> GoalPublicationSnapshot:
        await dispatch(self)
        return self.snapshot
