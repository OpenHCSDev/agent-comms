"""Server-only order, board action, owner-read, and emergency-release tool implementations."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from agent_comms.acp_extension import (
    GoalSnapshotReply,
    TurnChangedUpdate,
    UpdateMetadata,
)
from agent_comms.comms import Comms
from agent_comms.coordination.work import CoordinationWork
from agent_comms.execution.turn_bound import LastTurns
from agent_comms.execution.turn_digests import OrderEditsRead, UnavailableOrderEdits
from agent_comms.input_attempt import UNOBSERVED_DELIVERY_SCOPE
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.messaging.bus_read import CapturesTranscriptPage, UiRead
from agent_comms.messaging.channel_targets import CommsTag
from agent_comms.messaging.read_basis import ReadAuthority
from agent_comms.planning.goal_actions import CompliedGoalAction, OwnerInvocable
from agent_comms.planning.order_story import OrderStory
from agent_comms.planning.orders import (
    JudgedCall,
    ProgressRead,
    ProgressSource,
    WaivedVerdict,
)
from agent_comms.planning.problem_number import ProblemNumber
from agent_comms.projects.work_branches import WorkBranchError
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.task_attachments.team_pings import TeamUpdate
from agent_comms.team.board_actions import BoardActionApplied
from agent_comms.team.pi_rule_gate import PiThreadGate
from agent_comms.team.team_hud import TeamBoardPublicationStore
from agent_comms.team.team_hud_records import Teammate, TeammateDetail, TeamOrderDetail
from agent_comms.transcript.transcripts import TranscriptRead
from agent_comms.transport.native_tools import CommunicationCall
from agent_comms.transport.runtime_server import AttachedRuntimeRequestContext
from agent_comms.transport.serving.served_requests import OwnedRead, OwnerLiveRead
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_request import (
    GoalsArea,
    HumanToolRequest,
    NativeToolRequest,
    ReadTool,
    ScreensArea,
    ThreadReadTool,
    ThreadsArea,
    ThreadToolRequest,
    WorkArea,
)
from agent_comms.transport.tools.orders import (
    CommsEmergencyReleaseTool,
    EmergencyRelease,
    EmergencyReleased,
    WaivedEdits,
)
from agent_comms.transport.tools.reads import (
    InputDeliveryRead,
    OwnerGoalRead,
    TeammateRead,
    TranscriptPageRead,
)
from agent_comms.transport.tools.sidebar import CommsBoardActionTool
from agent_comms_hooks import ToolRefusedError


@dataclass(frozen=True, kw_only=True)
class ServedCommsBoardActionTool(
    ScreensArea, HumanToolRequest[BoardActionApplied], CommsBoardActionTool
):
    """Serving CommsBoardActionTool."""

    def answered(self, comms: Comms) -> BoardActionApplied:
        recipient = self.offer.recipient
        if not recipient.current(comms.registry.snapshot()):
            raise ToolRefusedError("The board action recipient incarnation changed")
        current = tuple(
            action
            for packet in TeamUpdate.latest(comms, recipient)
            for ping in packet.handled
            for action in ping.board_actions(packet.teammate)
        )
        if self.offer not in current:
            raise ToolRefusedError("The board action is stale; read the current board")
        return self.offer.execute(comms.root, recipient.name)


class ServedInputDeliveryRead(ThreadsArea, OwnedRead, InputDeliveryRead):
    """Serving the delivery projection: the owner's queue when observed, else the stored one."""

    def unavailable(self, comms: Comms, owner: ThreadIncarnation) -> DeliveryOverview:
        return comms.goals.input_delivery(
            owner.name, include_history=self.include_history, scope=UNOBSERVED_DELIVERY_SCOPE,
        )

    async def owned(self, ctx: AttachedRuntimeRequestContext) -> DeliveryOverview:
        scope = await ctx.server.agent.inputs.delivery_scope(ctx.session)
        return await CoordinationWork.run_worker(
            partial(
                ctx.server.agent._comms.goals.input_delivery,
                ctx.name,
                include_history=self.include_history,
                scope=scope,
            )
        )


class ServedOwnerGoalRead(GoalsArea, OwnerLiveRead, OwnerGoalRead):
    """Serving the goal snapshot from the live native owner, with the turn it read; an
    external CLI's goal is the bus's own, read with no native turn."""

    def held(self, comms: Comms, owner: ThreadIncarnation) -> GoalSnapshotReply:
        return GoalSnapshotReply.from_snapshot(
            comms.goals.goal_snapshot(owner.name), UpdateMetadata.of()
        )

    async def owned(self, ctx: AttachedRuntimeRequestContext) -> GoalSnapshotReply:
        snapshot = await CoordinationWork.run_worker(
            partial(
                ctx.server.agent._comms.goals.goal_snapshot,
                ctx.name,
            )
        )
        turn = await CoordinationWork.run_worker(
            partial(
                ctx.server.agent.turns.turn_state,
                ctx.session,
            )
        )
        return GoalSnapshotReply.from_snapshot(snapshot, UpdateMetadata.of(TurnChangedUpdate(turn)))


class ServedTranscriptPageRead(
    ThreadsArea, CapturesTranscriptPage, ThreadReadTool, TranscriptPageRead
):
    """Serving one transcript page under the reader's authority."""

    def capture_as(self, comms: Comms, reader: ReadAuthority) -> TranscriptRead:
        return comms.transcripts.capture_page_read(
            reader.subject(self.thread),
            position=self.position,
            limit=self.limit,
            page_source=self.page_source,
        )


class ServedTeammateRead(WorkArea, UiRead, ReadTool, NativeToolRequest, TeammateRead):
    """Serving one teammate card, refused when the frame or the card moved on."""

    def apply(self, comms: Comms, reader: ReadAuthority) -> TeammateDetail:
        snapshot = comms.registry.snapshot()
        if not self.frame_owner.current(snapshot) or not self.unit.current(snapshot):
            raise ToolRefusedError(
                "The selected teammate or teammate incarnation is no longer current"
            )
        hud = TeamBoardPublicationStore.read_for(comms, self.frame_owner.name)
        hud.require_current()
        for card in hud.units:
            if card.unit == self.unit:
                return self.detail(comms, card)
        raise ToolRefusedError(
            "The selected teammate is no longer in this teammate's published frame"
        )

    def detail(self, comms: Comms, card: Teammate) -> TeammateDetail:
        """Expand the published card this read selected from its original record owners."""
        if card.order_ids != self.orders:
            raise ToolRefusedError("The selected teammate's orders changed; read its current card")
        store = comms.goals.todos
        held = store.held_by(card.unit)
        current = {todo.id: todo for todo in held}
        if {(todo.id, todo.order.checked().id) for todo in held} != {
            (order.problem, order.order) for order in card.orders
        }:
            raise ToolRefusedError(
                "The selected teammate's assignments changed; read its current card"
            )
        pulls = ProgressRead(held)
        details = tuple(
            TeamOrderDetail(
                order,
                (current[order.problem].order.checked().instructions(current[order.problem]),),
                OrderStory.collect(order.order, store.for_order(order.order)),
                tuple(ProgressSource.of(current[order.problem], comms.registry, pulls).commits()),
            )
            for order in card.orders
        )
        turns = comms.transcripts.turns(card.unit.name, LastTurns())
        snapshot = comms.registry.snapshot()
        if turns.thread != card.unit or not card.unit.current(snapshot):
            raise ToolRefusedError("The selected incarnation changed while its details were read")
        return TeammateDetail(card, details, turns.to_wire())


class ServedCommsEmergencyReleaseTool(WorkArea, ThreadToolRequest, CommsEmergencyReleaseTool):
    """Serving CommsEmergencyReleaseTool."""

    native_call = CommunicationCall

    def apply(self, comms: Comms, caller: ThreadCaller) -> EmergencyReleased:
        actor = caller.identity(comms)
        PiThreadGate.of(comms, comms.registry.require(actor.name)).disable()
        waived: list[WaivedEdits] = []
        unreadable: list[UnavailableOrderEdits] = []
        verdicts: list[JudgedCall] = []
        for read in OrderEditsRead.for_team(comms.goals, caller.name):
            try:
                edits = read.read()
            except (WorkBranchError, OSError) as error:
                unreadable.append(UnavailableOrderEdits(
                    ProblemNumber.parse(read.todo.id), read.thread.incarnation, error))
                continue
            if not edits:
                continue
            waived.append(WaivedEdits(
                ProblemNumber.parse(read.todo.id), tuple(edit.id for edit in edits)))
            verdicts.extend(JudgedCall(read.todo.id, WaivedVerdict(
                read.todo.revision, actor, read.todo.held(), edit.id)) for edit in edits)
        if verdicts:
            comms.goals.judge_calls(tuple(verdicts), actor)
        held = tuple(
            thread.incarnation
            for thread in comms.registry.snapshot().threads.values()
            if any(goal.state.lifts_on_compliance for goal in thread.goal)
        )
        release = EmergencyRelease(held, tuple(waived), tuple(unreadable))
        notice = comms.messaging.send(
            caller.name, CommsTag.channel(),
            release.text(caller.name) + " The caller's session rule gate is off; saved evidence stays.",
        ).message
        for identity in held:
            comms.goals.update_goal(
                identity.name, CompliedGoalAction(witness=notice.message_id),
                actor=OwnerInvocable,
            )
        return EmergencyReleased(notice.message_id, release)
