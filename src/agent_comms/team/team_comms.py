"""The state owners shared by team commands and the full Core composition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import cached_property
from pathlib import Path

from agent_comms.installer.active_route import CommsRoute, resolve_comms_route
from agent_comms.messaging.publisher import OwnerPublisher
from agent_comms.messaging.published_bus import LivePublishedBus, PublishedBus
from agent_comms.messaging.sending import Messaging
from agent_comms.planning.goal_management import Goals
from agent_comms.registry.route_client import ActivityClient


class TeamComms(ActivityClient, ABC):
    """The team-command capability: registry, goals, messaging, and live publications."""

    messaging: Messaging

    def __init__(self, route: CommsRoute) -> None:
        super().__init__(route)
        self.goals = Goals(self.root, self.registry, self.bus, self.agents)
        self.messaging = Messaging(self.root, self.registry, self.bus, self.goals)

    @property
    @abstractmethod
    def bus(self) -> PublishedBus[OwnerPublisher]: ...


class CoreComms(TeamComms, ABC):
    """The full Core capability: lifecycle, transcript, storage, and team state."""


class TeamClient(TeamComms):
    """A standalone team command, without transcript, UI, ACP, or Core runtime state."""

    @cached_property
    def bus(self) -> LivePublishedBus[OwnerPublisher]:
        return LivePublishedBus(self.root, self.registry, OwnerPublisher)


def team_wire(root: Path | str | None = None) -> TeamClient:
    """Build the team-command composition from an explicit or published root."""
    return TeamClient(resolve_comms_route(root))
