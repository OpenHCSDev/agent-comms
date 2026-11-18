from __future__ import annotations

from dataclasses import dataclass


from agent_comms.execution.session_updates import AgentCommsUpdate
from agent_comms.messaging.routing import MessageRoute, NoMessageRoute

@dataclass(frozen=True)
class TextRouteUpdate(AgentCommsUpdate):
    route: MessageRoute | NoMessageRoute = NoMessageRoute()
