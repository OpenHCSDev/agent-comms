"""Who a connector request acts as."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from .bus_publication import DirectorOrigin
from .comms import Comms
from .field_codec import FieldCodec, MalformedRecord
from .head_review import PullRequestHead
from .thread_identity import ThreadIncarnation
from .todos import DirectiveText, HeadReviewOrder
from .tool_caller import CommanderForkCaller, NoThreadCaller, ThreadCaller, ToolCaller
from .tool_request import ToolArgumentsRefusedError, ToolRequest


@dataclass(frozen=True, kw_only=True)
class ActingFork:
    fork: CommanderForkCaller | NoThreadCaller = NoThreadCaller()


@dataclass(frozen=True)
class ConnectorCaller(ThreadCaller):
    """A registered thread calling through the connector with its own grant."""

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ToolRequest.members_with(ToolRequest)}

    def acting(self, arguments: Mapping[str, object]) -> tuple[ToolCaller, Mapping[str, object]]:
        return self, arguments

    def input_schema(self, tool: type[ToolRequest] | type[ConnectorTool]) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(tool)


class ForkingCaller(ConnectorCaller):
    """A thread grant; its holder may act as one of its own registered commander forks."""

    def acting(self, arguments: Mapping[str, object]) -> tuple[ToolCaller, Mapping[str, object]]:
        keys = {plan.key for plan in FieldCodec.field_plans(ActingFork)}
        selected = {key: value for key, value in arguments.items() if key in keys}
        rest = {key: value for key, value in arguments.items() if key not in keys}
        try:
            acting_fork = FieldCodec.decode(ActingFork, selected)
        except MalformedRecord as error:
            raise ToolArgumentsRefusedError(
                f"Invalid connector caller arguments: {error}"
            ) from error
        return acting_fork.fork.acting_caller(self), rest

    def input_schema(self, tool: type[ToolRequest] | type[ConnectorTool]) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(tool, ActingFork)


@dataclass(frozen=True)
class DirectorCaller(ConnectorCaller):
    """claude-ai speaks for the owner, so its sends carry the director origin, which records each
    message to a thread as a directive like the owner's own."""

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText."""
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
