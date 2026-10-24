"""Who a connector request acts as."""

from __future__ import annotations

from dataclasses import dataclass

from .bus_publication import DirectorOrigin
from .comms import Comms
from .head_review import PullRequestHead
from .thread_identity import ThreadIncarnation
from .todos import DirectiveText, HeadReviewOrder
from .tool_caller import ThreadCaller
from .tool_request import ToolRequest


@dataclass(frozen=True)
class ConnectorCaller(ThreadCaller):
    """A registered thread calling through the connector with its own grant."""

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ToolRequest.members_with(ToolRequest)}


@dataclass(frozen=True)
class DirectorCaller(ConnectorCaller):
    """claude-ai speaks for the owner, so its sends carry the director origin, which records each
    message to a thread as a directive like the owner's own."""

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText. claude-ai directs the commanders, so the
        commander rules review does not gate it."""
        DirectiveText.require(body)

    def origin(self) -> DirectorOrigin:
        return DirectorOrigin()

    def head_review_actor(self, comms: Comms, request: PullRequestHead,
                          order: HeadReviewOrder) -> ThreadIncarnation:
        return order.director_identity(comms.goals, self.identity(comms))

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return super().exposed() | {
            tool.declared_name: tool for tool in ConnectorTool.members_with(ConnectorTool)
        }


class ConnectorTool(ToolRequest[DirectorCaller], affix="ConnectorTool"):
    """Connector-only declarations are absent from the native/CLI tool registry."""


CALLER = DirectorCaller("claude-ai")  # Owner-typed calls act as this registered bus thread.
