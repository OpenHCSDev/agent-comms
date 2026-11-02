"""Who a connector request acts as."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_comms_hooks import ToolRefusedError
from pydantic import JsonValue

from .bus_publication import DirectorOrigin
from .comms import Comms
from .nominal.field_codec import FieldCodec, MalformedRecord, RootId
from .registry_store import RegistryStore
from .todos import DirectiveText, JudgedCall, WaivedVerdict
from .channel_targets import CommsTag
from .goal_actions import CompliedGoalAction, OwnerInvocable
from .turn_digests import OrderEditsRead
from .work_branches import WorkBranchError
from .read_basis import OperatorOutcomes
from .tool_caller import ForkCaller, NoThreadCaller, ThreadCaller, ToolCaller
from .tool_request import ReadTool, ToolArgumentsRefusedError, ToolRequest
from .tool_result import ToolResult


class GrantRefusedError(ToolRefusedError):
    """The bearer token identifies no live caller authorized for the served root."""


@dataclass(frozen=True, kw_only=True)
class ActingFork:
    fork: ForkCaller | NoThreadCaller = NoThreadCaller()


class ConnectorCaller(ToolCaller):
    """The MCP contract, independent of whether its principal is a bus thread."""

    def require_root(self, root: Path) -> None:
        """Existing thread and Director grants keep their current root access."""

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        """A thread-free, unscoped caller has no membership to publish."""

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ToolRequest.members_with(ToolRequest)}

    def acting(self, arguments: Mapping[str, object]) -> tuple[ToolCaller, Mapping[str, object]]:
        return self, arguments

    def input_schema(self, tool: type[ToolRequest] | type[ConnectorTool]) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(tool)


@dataclass(frozen=True)
class ThreadConnectorCaller(ConnectorCaller, ThreadCaller):
    """A registered thread calling through MCP with its own grant."""

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        self.identity(comms)
        acting.join(comms, self.own_thread())


@dataclass(frozen=True)
class ReadOnlyConnectorCaller(ConnectorCaller, NoThreadCaller):
    """A thread-free MCP caller with the declared read capability."""

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ToolRequest.members_with(ReadTool)}

    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult:
        request.require_read_only()
        self.require_root(comms.root)
        return super().invoke(request, comms)


@dataclass(frozen=True)
class UiReadCaller(ReadOnlyConnectorCaller):
    """The provisioned UI reader of an existing original native namespace."""

    namespace: RootId

    def require_root(self, root: Path) -> None:
        RegistryStore.at(root).privacy().require_root(
            self.namespace, GrantRefusedError("UI grant belongs to a different root."))

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        """Recheck the opened root before argument context reads or tool execution."""
        self.require_root(comms.root)


class ForkingCaller(ThreadConnectorCaller):
    """A thread grant; its holder may act as one of its own registered teammate forks."""

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
class DirectorCaller(OperatorOutcomes, ThreadConnectorCaller):
    """claude-ai speaks for the owner, so its sends carry the director origin, which records each
    message to a thread as a directive like the owner's own."""

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText."""
        DirectiveText.require(body)

    def origin(self) -> DirectorOrigin:
        return DirectorOrigin()

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return super().exposed() | {
            tool.declared_name: tool for tool in ConnectorTool.members_with(ConnectorTool)
        }


class ConnectorTool(ToolRequest[DirectorCaller], affix="ConnectorTool"):
    """Connector-only declarations are absent from the native/CLI tool registry."""


@dataclass(frozen=True)
class EmergencyReleased(ToolResult):
    """Whose chokehold lifted and how many unjudged edits were waived, per problem."""

    notice: str
    released: tuple[str, ...]
    waived: dict[str, int]
    unreadable: dict[str, str]
    """Orders whose edits could not be read, with why: their edits still count."""


@dataclass(frozen=True, kw_only=True)
class CommsEmergencyReleaseTool(ConnectorTool, declared_name="comms_emergency_release"):
    """Tristan's emergency button. The rules stay: only today's pile and today's holds go."""

    label = "Emergency release"
    description = (
        "Tristan's emergency button: lift every chokehold and waive every unjudged edit as of "
        "now. Posts the release to #comms; waived edits are neither approved nor struck, and "
        "new edits and vibe checks count again afterwards.")

    def apply(self, comms: Comms, caller: DirectorCaller) -> EmergencyReleased:
        actor = caller.identity(comms)
        verdicts: list[JudgedCall] = []
        waived: dict[str, int] = {}
        unreadable: dict[str, str] = {}
        for read in OrderEditsRead.for_team(comms, caller.name):
            try:
                edits = read.read()
            except (WorkBranchError, OSError) as error:
                unreadable[read.todo.id] = str(error)
                continue
            for edit in edits:
                verdicts.append(JudgedCall(edit.problem, WaivedVerdict(
                    read.todo.revision, actor, read.todo.held(), edit.id)))
                waived[edit.problem] = waived.get(edit.problem, 0) + 1
        if verdicts:
            comms.goals.judge_calls(tuple(verdicts), actor)
        snapshot = comms.registry.snapshot()
        held = tuple(thread.name for thread in snapshot.threads.values()
                     if any(goal.state.lifts_on_compliance for goal in thread.goal))
        notice = comms.messaging.send(caller.name, CommsTag.channel(), (
            "Emergency release (Tristan): every chokehold is lifted"
            f"{' (' + ', '.join(held) + ')' if held else ''} and {sum(waived.values())} unjudged"
            " edits are waived as of now. Nobody forks judges for the old pile. Waived is not"
            " approved; new edits count again."
            f"{' Unreadable, still counting: ' + ', '.join(unreadable) if unreadable else ''}")).message
        for name in held:
            comms.goals.update_goal(name, CompliedGoalAction(witness=notice.message_id),
                                    actor=OwnerInvocable)
        return EmergencyReleased(notice.message_id, held, waived, unreadable)


CALLER = DirectorCaller("claude-ai")  # Owner-typed calls act as this registered bus thread.
