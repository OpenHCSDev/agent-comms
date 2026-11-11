"""The goal tools: what a caller publishes about a goal, and the verbs that change one.

Separate from ``agent_comms.transport.tools.served`` because every consumer of these classes is a goal writer or
the goal snapshot a heartbeat publishes; the served wiring for the other tools stays there.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Annotated, ClassVar

from pydantic import JsonValue

from agent_comms.comms import Comms
from agent_comms.execution.input_key import InputKeyText
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GoalId,
    NullMember,
    SingleFieldValue,
    WireChoices,
    WireExclude,
    WireName,
    projected,
)
from agent_comms.nominal.lifecycle_capability import Active
from agent_comms.pi_payloads import PublishedGoalToolDetails
from agent_comms.planning.goal_actions import (
    ActiveGoalAction,
    ClearGoalAction,
    EditGoalAction,
    GoalAction,
    GoalActor,
    GoalPrecondition,
    ModelInvocable,
    PausedGoalAction,
    PeerInvocable,
    RuntimeInvocable,
    SetGoalAction,
    TransitionGoalAction,
)
from agent_comms.planning.goal_execution import GoalExecution
from agent_comms.planning.goal_history import RecordedGoalHistory
from agent_comms.planning.goal_presentation import GoalSnapshot
from agent_comms.planning.goal_scope import AllGoals, OneGoal
from agent_comms.planning.goals import GoalDeclaration
from agent_comms.planning.progress_claims import ProgressClaim
from agent_comms.transport.request import ListedNames, OmittedNames, tool_field
from agent_comms.transport.tool_caller import ThreadCaller, ToolCaller
from agent_comms.transport.tool_request import ThreadReadTool, ThreadToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError


class OwnGoalPublication(ToolResult):
    """A self-relative goal writer returns the exact declaration it published."""

    __slots__ = ()

    @projected(view=WireExclude, name="agentCommsKind")
    def publication_kind(self) -> object:
        return FieldCodec.encode(PublishedGoalToolDetails)


@dataclass(frozen=True)
class GoalSet(OwnGoalPublication):
    goal: GoalDeclaration
    instruction: str


@dataclass(frozen=True)
class GoalProgress(OwnGoalPublication, GoalSnapshot):
    execution: Annotated[GoalExecution, WireName("goal_execution")]


@dataclass(frozen=True)
class GoalUpdated(OwnGoalPublication):
    goal: GoalDeclaration


@dataclass(frozen=True)
class TeammateGoalChanged(ToolResult):
    """A teammate's goal as this caller left it. It is never the caller's own publication, so
    the caller's turn permission does not take it."""

    thread: str
    goal: GoalDeclaration


@dataclass(frozen=True, slots=True)
class OwnGoal(NullMember):
    """No thread was named: the caller's own goal."""

    actor: ClassVar[type[GoalActor]] = RuntimeInvocable

    def thread_name(self, caller: ThreadCaller) -> str:
        return caller.name

    def published(self, own: ToolResult, goal: GoalDeclaration) -> ToolResult:
        return own


@dataclass(frozen=True, slots=True)
class TeammateGoal(SingleFieldValue):
    """A teammate's goal, changed under PeerInvocable's consent rule."""

    thread: str
    actor: ClassVar[type[GoalActor]] = PeerInvocable

    def thread_name(self, caller: ThreadCaller) -> str:
        return self.thread

    def published(self, own: ToolResult, goal: GoalDeclaration) -> ToolResult:
        return TeammateGoalChanged(self.thread, goal)


@dataclass(frozen=True, kw_only=True)
class GoalChangeTool(ThreadToolRequest):
    """Your own goal, or a teammate's when thread names one: responsibility for goals is
    shared, and PeerInvocable owns what a teammate may not change."""

    thread: Annotated[
        OwnGoal | TeammateGoal, *tool_field("Teammate whose goal to change; omit for your own")
    ] = OwnGoal()

    def update(self, comms: Comms, caller: ThreadCaller, action: GoalAction) -> GoalDeclaration:
        return comms.goals.update_goal(
            self.thread.thread_name(caller), action, actor=self.thread.actor)

    def current(self, comms: Comms, caller: ThreadCaller, goal_id: GoalId):
        return comms.registry.require(self.thread.thread_name(caller)).goal.require().require_id(goal_id)


@dataclass(frozen=True, kw_only=True)
class CommsSetGoalTool(GoalChangeTool):
    label = "Set persistent goal"
    description = (
        "Set or replace your own persistent goal, or a teammate's when thread names one. "
        "Use this when the user asks you to start, set, or pursue a goal autonomously."
    )
    text: Annotated[str, *tool_field("Persistent objective to pursue")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        goal = self.update(comms, caller, SetGoalAction(text=self.text))
        return self.thread.published(GoalSet(
            goal, "Persistent goal activated. Work toward it and report progress with comms_goal."),
            goal)


@dataclass(frozen=True, kw_only=True)
class CommsGoalTool(ThreadToolRequest):
    label = "Goal progress"
    description = (
        "Update goal progress; complete it or block it when user input is needed. Use "
        "standby with explicit wait_for thread names when no independent work remains "
        "and a named collaborator must supply the next result. "
        "The goal remains active, but only a direct message from a named dependency or "
        "a user follow-up starts its next turn. Do not repeatedly announce waiting or "
        "return empty output. If existing dependency replies block standby, inspect "
        "comms_inbox with standby {goal_id, wait_for}. Read standby_review.messages and "
        "copy only standby_review.reviewed_inputs into reviewed_inputs; this records "
        "your decision to wait for a later reply without replaying uncertain inputs."
    )
    goal_id: Annotated[GoalId, *tool_field("Goal identity provided in the turn context")]
    status: Annotated[
        type[TransitionGoalAction], *tool_field("Goal state", WireChoices(GoalAction.model_choices))
    ]
    progress: Annotated[
        str, *tool_field("Progress summary; blocked requires a nonempty explicit reason")
    ]
    wait_for: Annotated[
        ListedNames | OmittedNames, *tool_field("Explicit thread names or @names; required for standby")
    ] = OmittedNames()
    reviewed_inputs: Annotated[
        ListedNames | OmittedNames,
        *tool_field(
            "Exact unresolved inputId keys inspected and handled for this goal; standby only"
        ),
    ] = OmittedNames()
    claims: Annotated[
        tuple[ProgressClaim, ...],
        *tool_field(
            "Typed evidence claims, each with the artifact Core verifies; "
            "a completed report needs at least one"
        ),
    ] = ()

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalProgress:
        name = caller.name
        self.status.require_claims(self.claims)
        ProgressClaim.require_all(self.claims, comms, comms.registry.require(name).worktree.path)
        command = replace(
            self.status(progress=self.progress),
            expect=self.status.model_precondition(self.goal_id),
            **{key: names for key, names in (
                ("wait_for", self.wait_for.names), ("reviewed_inputs", tuple(
                    InputKeyText.from_text(key) for key in self.reviewed_inputs.names
                )),
            ) if names},
        )
        return GoalProgress.from_snapshot(
            comms.goals.update_goal_snapshot(name, command, actor=ModelInvocable))


@dataclass(frozen=True, kw_only=True)
class CommsResumeGoalTool(GoalChangeTool):
    label = "Resume paused goal"
    description = (
        "Resume a paused goal, your own or a teammate's, with the same ID only after an explicit "
        "request. This does not replace the goal or resume blocked/completed goals."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the paused goal to resume")]
    progress: Annotated[str, *tool_field("Progress summary for the resumed goal")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        goal_id = self.goal_id
        current = self.current(comms, caller, goal_id)
        current.state.require_model_resume()
        progress = self.progress
        goal = self.update(
            comms, caller,
            ActiveGoalAction(
                expect=GoalPrecondition(
                    expected_checkpoint=current.captured,
                    goal_id=OneGoal(goal_id),
                ),
                progress=progress,
            ),
        )
        goal = goal.require()
        if not goal.state.items_with(Active, goal) or goal.progress != progress:
            raise ToolRefusedError("Goal changed during resume; refresh its state.")
        return self.thread.published(GoalUpdated(goal), goal)


@dataclass(frozen=True, kw_only=True)
class CommsEditGoalTool(GoalChangeTool):
    label = "Edit goal text"
    description = (
        "Edit an existing goal's text, your own or a teammate's, while keeping its identity, "
        "status, and progress. Use comms_set_goal only to replace the objective with a fresh "
        "goal ID."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the existing goal to edit")]
    text: Annotated[str, *tool_field("Revised objective text, including any @mentions")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        goal_id = self.goal_id
        current = self.current(comms, caller, goal_id)
        goal = self.update(
            comms, caller,
            EditGoalAction(
                expect=GoalPrecondition(expected_checkpoint=current.captured, goal_id=OneGoal(goal_id)), text=self.text
            ),
        )
        return self.thread.published(GoalUpdated(goal), goal)


@dataclass(frozen=True, kw_only=True)
class CommsClearGoalTool(GoalChangeTool):
    label = "Clear goal"
    description = (
        "Clear a goal, your own or a teammate's, leaving that thread with no goal. Held "
        "problems must be finished or released first; a goal Tristan paused, or the chokehold "
        "holds, cannot be cleared by a teammate."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the goal to clear")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        current = self.current(comms, caller, self.goal_id)
        goal = self.update(
            comms, caller,
            ClearGoalAction(expect=GoalPrecondition(
                expected_checkpoint=current.captured, goal_id=OneGoal(self.goal_id))),
        )
        return self.thread.published(GoalUpdated(goal), goal)


@dataclass(frozen=True, kw_only=True)
class CommsPauseGoalTool(GoalChangeTool):
    label = "Pause goal"
    description = (
        "Pause a goal, your own or a teammate's, keeping its identity; comms_resume_goal "
        "resumes it. Pausing an already-paused goal keeps its original pause."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the goal to pause")]
    progress: Annotated[str, *tool_field("Why the goal is paused")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        current = self.current(comms, caller, self.goal_id)
        goal = self.update(
            comms, caller,
            PausedGoalAction(
                expect=GoalPrecondition(expected_checkpoint=current.captured, goal_id=OneGoal(self.goal_id)),
                progress=self.progress,
            ),
        )
        return self.thread.published(GoalUpdated(goal), goal)


@dataclass(frozen=True, kw_only=True)
class CommsGoalHistoryTool(ThreadReadTool):
    label = "Goal history"
    description = "Read recorded goal revisions and recorded provenance and observation gaps."
    goal_id: Annotated[
        OneGoal | AllGoals, *tool_field("Optional goal ID; omit for this thread's full history")
    ] = AllGoals()

    def apply(self, comms: Comms, caller: ToolCaller) -> RecordedGoalHistory:
        return comms.goals.recorded_history(caller.subject(self.thread), self.goal_id)
