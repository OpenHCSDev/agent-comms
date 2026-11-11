from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Annotated

from pydantic import JsonValue

from agent_comms.acp_extension import (
    GoalSnapshotReply,
    TurnChangedUpdate,
    UpdateMetadata,
)
from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.errors import TodoError
from agent_comms.execution.turn_bound import LastTurns, TurnBound, TurnsSince
from agent_comms.execution.turn_bound import (
    LastTurns,
    TurnBound,
    TurnsSince,
)
from agent_comms.execution.turn_digests import (
    ActivityTurnView,
    OrderEditsRead,
    ThreadTurns,
    TurnView,
    UnavailableOrderEdits,
    UnjudgedEdits,
)
from agent_comms.input_attempt import UNOBSERVED_DELIVERY_SCOPE
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.messaging.bus_read import (
    CapturesTranscriptPage,
    OwnerScopedRead,
    UiRead,
)
from agent_comms.messaging.channel_targets import CommsTag
from agent_comms.messaging.read_basis import ReadAuthority
from agent_comms.messaging.send_result import MessagePublished
from agent_comms.nominal.field_codec import (
    FieldCodec,
    MessageId,
    OrderId,
    RecordProblems,
    ToolCallId,
)
from agent_comms.planning.delivery_evidence import TodoProblems
from agent_comms.planning.goal_actions import (
    CompliedGoalAction,
    CreateProblemGoalAction,
    OrderGoalAction,
    OwnerInvocable,
)
from agent_comms.planning.goals import Goal, OrderCheck
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.order_phase import OrderPhase
from agent_comms.planning.order_story import OrderStory, TeamStory
from agent_comms.planning.order_words import DeliveryReport, PlainText, StrikeReason
from agent_comms.planning.order_work import OrderWork
from agent_comms.planning.orders import (
    AssignedClaimReviewer,
    ClaimOrder,
    ClaimReviewer,
    CorrectCheckTodo,
    CorrectRunTodo,
    DeliverTodo,
    HeadReviewed,
    HeadVerdict,
    InstallTodo,
    JudgedCall,
    MeasuredReview,
    NoHeadOrder,
    OrderClaim,
    OrderCompletion,
    OrderProgress,
    ProgressRead,
    ProgressSource,
    ResumeClaimOrder,
    ServeTodo,
    Todo,
    TodoOperation,
    ToolCallVerdict,
    WaivedVerdict,
)
from agent_comms.planning.problem_number import ProblemNumber
from agent_comms.planning.progress_claims import ProgressClaim
from agent_comms.planning.pull_requests import PullRequestLink
from agent_comms.projects.head_review import (
    HeadReviewOrder,
    PreparedHeadReview,
    PullRequestHead,
)
from agent_comms.projects.work_branches import WorkBranchError
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.team.team_hud import TeamFrame, TeamHud, TeamLoad
from agent_comms.team.team_hud_records import Teammate, TeammateDetail, TeamOrderDetail
from agent_comms.transcript.transcripts import TranscriptRead
from agent_comms.transport.request import tool_field
from agent_comms.transport.runtime_server import AttachedRuntimeRequestContext
from agent_comms.transport.serving.served_requests import OwnedRead, OwnerLiveRead
from agent_comms.transport.tool_caller import ThreadCaller, ToolCaller
from agent_comms.transport.tool_declaration import EditCall
from agent_comms.transport.tool_request import (
    NativeToolRequest,
    PublishingToolRequest,
    ReadTool,
    ThreadReadTool,
    ThreadToolRequest,
)
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools.reads import (
    InputDeliveryRead,
    OwnerGoalRead,
    TeammateRead,
    TranscriptPageRead,
)
from agent_comms_hooks import ToolRefusedError


@dataclass(frozen=True)
class TodoChanged(ToolResult):
    todo: Todo
    strike_count: int


@dataclass(frozen=True)
class TodoList(ToolResult):
    todos: tuple[Todo, ...]
    progress: dict[str, OrderProgress] = field(default_factory=dict)


@dataclass(frozen=True)
class ProblemOrdered(ToolResult):
    goal: Goal
    todos: tuple[Todo, ...]


@dataclass(frozen=True)
class ProblemClosed(ToolResult):
    todo: Todo


@dataclass(frozen=True)
class ProblemNumberReserved(ToolResult):
    problem: str


@dataclass(frozen=True, kw_only=True)
class CommsTodoTool(ThreadToolRequest):
    label = "Change problem"
    description = (
        "Apply an exact revision-checked Todo command; only its teammate may invoke this tool."
    )
    problem: Annotated[str, *tool_field("Problem ID")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoChanged | ProblemClosed:
        todo = comms.goals.todos.get(self.problem)
        actor = caller.identity(comms)
        return self.result(comms.goals.change_problem(
            self.problem, self.operation_for(todo, actor, comms), actor))

    @abstractmethod
    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        """Build the change, verifying any attached application claims before writing."""

    def result(self, changed: Todo) -> TodoChanged | ProblemClosed:
        return TodoChanged(changed, changed.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsChangeProblemTool(CommsTodoTool, declared_name="comms_todo"):
    operation: Annotated[
        TodoOperation, *tool_field("Typed assignment, strike, release or state command")
    ]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return self.operation.authored_by(actor)


@dataclass(frozen=True, kw_only=True)
class CommsVerdictTool(ThreadToolRequest):
    """One verdict is a batch of one: Goals.judge_calls owns checking and recording."""
    label = "Judge order tool call"
    description = (
        "Record the teammate's verdict on one tool call the holder made for its "
        "order: an accepted verdict quotes 4+ words of the order; a send-back strikes "
        "the holder and revises its Goal in the same write."
    )
    problem: Annotated[str, *tool_field("Problem ID")]
    operation: Annotated[
        ToolCallVerdict, *tool_field("Accepted or send-back verdict on the judged tool call ID")
    ]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoChanged:
        actor = caller.identity(comms)
        changed, = comms.goals.judge_calls(
            (JudgedCall(self.problem, self.operation.authored_by(actor)),), actor)
        return TodoChanged(changed, changed.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsVerdictsTool(ThreadToolRequest):
    label = "Judge many order tool calls"
    description = (
        "Record many verdicts in one call: each is checked against its order before any is "
        "written and every refusal is listed together; accepted, superseded, rewritten and "
        "dropped verdicts land in one transaction, and each send-back strikes with its Goal."
    )
    verdicts: Annotated[tuple[JudgedCall, ...], *tool_field("One verdict per judged tool call")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoList:
        actor = caller.identity(comms)
        return TodoList(comms.goals.judge_calls(
            tuple(replace(call, operation=call.operation.authored_by(actor))
                  for call in self.verdicts),
            actor))


@dataclass(frozen=True, kw_only=True)
class HeadVerdictTool:
    label = "Review a PR head"
    description = (
        "Record acceptance or rejection of an exact repository/PR/head and post "
        "agent-comms/review. An order link is optional; linked reviews quote 4+ "
        "words of the order, and rejection strikes its holder in the same write."
    )
    request: Annotated[PullRequestHead, *tool_field("Repository, PR number and reviewed head")]
    operation: Annotated[HeadVerdict, *tool_field("Accepted or rejected head verdict")]
    order: Annotated[HeadReviewOrder, *tool_field("Optional held-order link")] = NoHeadOrder()

    def apply(self, comms: Comms, caller: ThreadCaller) -> HeadReviewed:
        actor = caller.head_review_actor(comms, self.request, self.order)
        problems: dict[str, str] = {}
        orders = self.order.prepared(comms.goals, problems)
        heads = []
        with RecordProblems.collecting(problems, '--head', 'supply the current PR head SHA with --head'):
            heads.append(self.operation.current_head(self.request))
        preparations = []
        for current in heads:
            with RecordProblems.collecting(problems, self.operation.preparation_field,
                                          self.operation.preparation_fix):
                preparations.append(PreparedHeadReview(current, self.operation.subject(comms.goals, current)))
        if problems:
            raise TodoProblems(problems)
        order, = orders
        prepared, = preparations
        return self.operation.reviewed(comms.goals, self.request, prepared, actor, order)


@dataclass(frozen=True, kw_only=True)
class CommsHeadVerdictTool(HeadVerdictTool, ThreadToolRequest):
    """The native/CLI member of the shared authenticated head-review operation."""


@dataclass(frozen=True, kw_only=True)
class CommsUnjudgedTool(ThreadReadTool):
    label = "Unjudged edits"
    description = "Read original edit calls since each open order, excluding that order's recorded verdicts."

    def apply(self, comms: Comms, caller: ToolCaller) -> UnjudgedEdits:
        owner = comms.registry.require(caller.subject(self.thread)).incarnation.require_recorded()
        passes = ServicePassStore.at(comms.root).read()
        edits: list[EditCall] = []
        unavailable: list[UnavailableOrderEdits] = []
        for read in OrderEditsRead.for_team(comms.goals, owner.name):
            try:
                edits.extend(read.read())
            except (WorkBranchError, OSError) as error:
                unavailable.append(UnavailableOrderEdits(
                    ProblemNumber.parse(read.todo.id), read.thread.incarnation, error))
        return UnjudgedEdits(tuple(edits), tuple(unavailable)).with_services(passes, owner)


@dataclass(frozen=True, kw_only=True)
class BoardReadTool(ThreadReadTool, declared_name=None):
    """A read of one teammate's board. An omitted thread reads the caller's own, and an
    empty board looks like a broken reader, so the reply names whose board it read."""

    def reply(self, read: dict[str, JsonValue], caller: ToolCaller) -> dict[str, JsonValue]:
        return {"board": caller.subject(self.thread), **read}


@dataclass(frozen=True, kw_only=True)
class CommsTeamLoadTool(BoardReadTool, declared_name="comms_team_load"):
    label = "Measured teammate load"
    description = "Read retained teammate load with recorded service failures and its original completion clock."

    def apply(self, comms: Comms, caller: ToolCaller) -> TeamLoad:
        return TeamHud.read(comms, caller.subject(self.thread)).load_reading()


@dataclass(frozen=True)
class TeamBoard(ToolResult):
    """The team board as Tristan reads it: the rendered text, without the records under it."""

    lines: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CommsBoardTool(BoardReadTool):
    label = "Team board"
    description = (
        "Read the team board as text: what needs action, stalls, PRs nobody owns, idle work "
        "and each teammate's card. comms_hud returns the records under it."
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> TeamBoard:
        return TeamBoard(TeamHud.read(comms, caller.subject(self.thread)).frame)


@dataclass(frozen=True, kw_only=True)
class CommsHudTool(ThreadReadTool):
    label = "Team HUD"
    description = (
        "Read the teammate's latest published Tristan frame: orders, review timers, "
        "strikes, edits, PRs, plan phases and load. Recorded failures precede retained data; "
        "stale rows stay explicitly stale, and missing frames remain unavailable."
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> TeamHud:
        return TeamHud.read(comms, caller.subject(self.thread))


@dataclass(frozen=True, kw_only=True)
class CommsFireTool(ThreadToolRequest):
    label = "Fire teammate"
    description = (
        "Release every open problem the teammate holds in one operation, recording one "
        "plain reason on each; only their teammate may invoke this tool."
    )
    unit: Annotated[str, *tool_field("Teammate thread to fire")]
    reason: Annotated[StrikeReason, *tool_field("What the teammate did, plainly")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoList:
        return TodoList(comms.goals.fire(self.unit, self.reason, caller.identity(comms)))


@dataclass(frozen=True, kw_only=True)
class CommsCorrectWordingTool(ThreadToolRequest):
    label = "Correct teammate wording"
    description = (
        "Answer a teammate's jargon with a correction naming its open orders; the teammate's "
        "strikes stay as they are. Only its teammate may invoke this tool."
    )
    unit: Annotated[str, *tool_field("Teammate thread whose message used jargon")]
    message_id: Annotated[MessageId, *tool_field("The teammate's message")]
    text: Annotated[str, *tool_field("The teammate's message text")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> MessagePublished:
        orders = [todo.id for todo in comms.goals.todos.board()
                  if todo.holder == self.unit]
        if not orders:
            raise ToolRefusedError(f"{self.unit} holds no open order from {caller.name}.")
        if not (words := PlainText.found(self.text)):
            raise ToolRefusedError(
                f"Message {self.message_id} is plain; there is nothing to correct."
            )
        result = comms.messaging.send(
            caller.name, self.unit,
            f"Correction, no strike, on {', '.join(orders)}: message {self.message_id} used "
            f"{', '.join(words)}. Say what you did and what broke in plain words from now on.")
        return MessagePublished.from_send(result)


@dataclass(frozen=True, kw_only=True)
class CommsTodoListTool(BoardReadTool):
    label = "Read problems"
    description = "Read the team's Todo orders, review timers, strikes and hunts."

    def apply(self, comms: Comms, caller: ToolCaller) -> TodoList:
        snapshot = comms.registry.snapshot()
        listed = tuple(comms.goals.todos.board())
        pulls = ProgressRead(listed)
        # An orphaned hold has no unit checkout to read; HolderGone reports it.
        held = HeldOrders.split(tuple(todo for todo in listed if todo.holder), snapshot)
        return TodoList(listed, {todo.id: OrderProgress.read(todo, comms.registry, pulls)
                                 for todo in held.todos})


@dataclass(frozen=True, kw_only=True)
class CommsStandingRulesTool(CommsHudTool):
    label = "Read broken standing rules"
    description = (
        "Read the published Tristan board and HUD. Tristan owns "
        "enforcement; this read never repeats it. Recorded failures precede stale retained rows; "
        "a missing frame remains explicitly unavailable."
    )


@dataclass(frozen=True, kw_only=True)
class CommsOrderTool(ThreadToolRequest):
    label = "Assign problem order"
    description = (
        "Reserve the order's Todo assignments in one transaction and publish its Goal with "
        "the teammate's review timer; does not replay input."
    )
    order: Annotated[
        OrderGoalAction, *tool_field(
            "Problem, request, expected_revision, review_at, teammate, and together: the "
            "order's other open problems with their expected revisions")
    ]
    owner: Annotated[str, *tool_field("Assignee thread")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProblemOrdered:
        order = self.order.bound_to(caller.identity(comms)).bound_in(
            comms.registry.snapshot().team_project)
        order = replace(order, review_at=order.review_at.continued_from(
            MeasuredReview.read(comms.goals.todos).review_at(order.order).at))
        goal = comms.goals.update_goal(self.owner, order)
        return ProblemOrdered(goal, tuple(comms.goals.todos.get(problem)
                                          for problem in self.order.problems()))


@dataclass(frozen=True, kw_only=True)
class CommsProblemNumberTool(ThreadToolRequest):
    label = "Reserve problem number"
    description = (
        "Reserve the next free problem number for comms_todo_create, the same allocation "
        "order send uses: concurrent callers each get a distinct number, so none guesses one."
    )

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProblemNumberReserved:
        return ProblemNumberReserved(comms.goals.todos.allocate_problem())


@dataclass(frozen=True, kw_only=True)
class CommsTodoCreateTool(CommsOrderTool):
    label = "Create problem"
    description = "Create a problem with its owner, checked order and review timer in one command."
    order: Annotated[
        CreateProblemGoalAction, *tool_field("Typed problem creation and order")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsOrderDeliverTool(PublishingToolRequest[ThreadCaller, TodoChanged], ThreadToolRequest):
    label = "Claim order delivery"
    description = (
        "Record the holder's delivery claim and answer its teammate. "
        "Only the holder may claim a pending order; this does not accept its delivery. "
        "Self-assigned work selects an independent reviewer explicitly."
    )
    problem: Annotated[str, *tool_field("Held problem ID")]
    pull_request: Annotated[PullRequestLink, *tool_field(
        "The delivering pull request: its repository (owner/name) and number"
    )]
    head: Annotated[str, *tool_field("Claimed head commit")]
    report: Annotated[DeliveryReport, *tool_field(
        "Delivery report: the slop you killed or the broken thing you fixed, with file:line, "
        "in the owner's voice"
    )]
    reviewer: Annotated[ClaimReviewer, *tool_field(
        "Assignment parent by default; self-assigned work selects an independent "
        "reviewer's exact name and birth. Saved claims retain this choice."
    )] = AssignedClaimReviewer()

    def publish(self, comms: Comms, caller: ThreadCaller) -> TodoChanged:
        todo = comms.goals.todos.get(self.problem)
        actor = comms.registry.require(caller.name).incarnation
        generation = todo.held().generation
        todo.require_assignee(actor, generation)
        reviewer = self.reviewer.identity(todo)
        if not comms.goals.agents.answers_later(reviewer, comms.registry.snapshot()):
            raise TodoError(
                f"@{reviewer.name} will not answer this review: it has no live bridge or process, "
                "so the claim would wait unjudged. Name a live teammate with `reviewer` "
                "(its exact name and birth)."
            )
        # The identical call resumes a retained claim's publication; nothing is claimed again.
        for claim in todo.order.team_claims():
            if claim.resumed_by(self.pull_request, self.head, self.report.text, self.reviewer):
                resumed = ResumeClaimOrder(todo.revision, actor, generation, claim).record(
                    comms.goals, todo.id)
                return TodoChanged(resumed, resumed.strike_count)
        claim = OrderClaim.for_order(
            todo, actor, self.pull_request, self.head, self.report.text,
            tuple(comms.bus.bus.lookup_message(answered).reference
                  for answered in todo.held().consent.answered(todo)),
            reviewer=self.reviewer,
        )
        changed = ClaimOrder(
            todo.revision, actor, generation, claim
        ).record(comms.goals, todo.id)
        return TodoChanged(changed, changed.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsOrderDoneTool(CommsTodoTool):
    label = "Check delivered order"
    description = "Verify and deliver an open order by its own ID; the teammate's Goal is unchanged."
    order_id: Annotated[OrderId, *tool_field("Exact open order ID")]
    delivery: Annotated[
        OrderCompletion,
        *tool_field("A checked implementation delivery for fix, or a report for triage"),
    ]
    claims: Annotated[
        tuple[ProgressClaim, ...],
        *tool_field(
            "Typed evidence claims, each with the artifact Core verifies in the order's "
            "checkout; at least one for fix, none for triage reports"
        ),
    ]

    @classmethod
    def context_types(cls, comms: Comms, arguments: Mapping[str, object]) -> Mapping[str, type[object]]:
        problem = FieldCodec.field_plan(cls, "problem").read(arguments)
        return {"delivery": comms.goals.todos.get(problem).order.checked().scope.delivery_class}

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        todo.require_maintainer(actor)
        todo.state.require_open(todo)
        order = todo.order.checked()
        order.require_delivery(self.order_id)
        problems = self.delivery.preparation_problems(todo)
        if problems:
            raise TodoProblems(problems)
        delivery = self.delivery.resolved_delivery(todo)
        required = order.scope.required_claims(
            delivery, self.delivery.completion_claims(delivery, order, self.claims))
        verified = ProgressClaim.require_all(required, comms, order.check.directory)
        return DeliverTodo(todo.revision, actor, delivery, self.order_id, verified_claims=verified)


@dataclass(frozen=True, kw_only=True)
class CommsOrderCheckTool(CommsTodoTool):
    label = "Correct order check"
    description = (
        "Replace the check of an open order, recording the old check, the new one and "
        "why; only its teammate may invoke this tool, and the order keeps its review timer "
        "and strikes."
    )
    check: Annotated[
        OrderCheck, *tool_field("The corrected check: directory, command and expected result")
    ]
    why: Annotated[str, *tool_field("Why the recorded check was wrong")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return CorrectCheckTodo(todo.revision, actor, self.why,
                                previous=todo.order.check, check=self.check)


@dataclass(frozen=True, kw_only=True)
class CommsOrderRunTool(CommsTodoTool):
    label = "Correct order run"
    description = (
        "Replace the run text that closes an open application order, recording the old run "
        "and why; only its teammate may invoke this tool, and the order keeps its review "
        "timer and strikes."
    )
    run: Annotated[str, *tool_field("The corrected run: what the installed app must show")]
    why: Annotated[str, *tool_field("Why the recorded run was wrong")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        previous = todo.order.checked().run
        return CorrectRunTodo(todo.revision, actor, self.why,
                              previous=previous, run=previous.retexted(self.run))


@dataclass(frozen=True, kw_only=True)
class CommsOrderServeTool(CommsTodoTool):
    label = "Name order phase"
    description = (
        "Name the plan phase an open order serves; only its teammate may invoke this "
        "tool, and the order keeps its id, review timer and strikes."
    )
    serves: Annotated[type[OrderPhase], *tool_field("The plan phase the order serves")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return ServeTodo(todo.revision, actor, self.serves)


@dataclass(frozen=True, kw_only=True)
class CommsProblemCloseTool(CommsTodoTool):
    label = "Close installed problem"
    description = (
        "Record installed application evidence and close only a problem with no open order."
    )
    works: Annotated[
        str, *tool_field("What worked in the app, quoting the order's application run")
    ]
    artifact: Annotated[str, *tool_field("Existing verification artifact file")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return InstallTodo(todo.revision, actor, self.works, self.artifact)

    def result(self, changed: Todo) -> ProblemClosed:
        return ProblemClosed(changed)


@dataclass(frozen=True, kw_only=True)
class CommsTurnsTool(ThreadReadTool):
    label = "Read turns"
    description = ("Read bounded conversation excerpts and tool-call outcomes. "
                   "Use detail=original for complete recorded events, including raw evidence. "
                   "Send the returned older window to read the next older page.")
    window: Annotated[
        TurnBound, *tool_field("Turn window; send back older for the next page")
    ] = field(default_factory=LastTurns)
    detail: Annotated[
        type[TurnView], *tool_field("Activity excerpts by default; original returns complete evidence")
    ] = ActivityTurnView

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadTurns:
        return replace(comms.transcripts.turns(caller.subject(self.thread), self.window),
                       detail=self.detail)


class ServedInputDeliveryRead(OwnedRead, InputDeliveryRead):
    """Serving the delivery projection: the owner's queue when observed, else the stored one."""

    def unavailable(self, comms: Comms, owner: ThreadIncarnation) -> DeliveryOverview:
        return comms.goals.input_delivery(
            owner.name, include_history=self.include_history, scope=UNOBSERVED_DELIVERY_SCOPE,
        )

    async def owned(self, ctx: AttachedRuntimeRequestContext) -> DeliveryOverview:
        scope = await ctx.server.agent.inputs.delivery_scope(ctx.session)
        return await Coordination.run_worker(
            partial(
                ctx.server.agent._comms.goals.input_delivery,
                ctx.name,
                include_history=self.include_history,
                scope=scope,
            )
        )


class ServedOwnerGoalRead(OwnerLiveRead, OwnerGoalRead):
    """Serving the goal snapshot from the live native owner, with the turn it read."""

    async def owned(self, ctx: AttachedRuntimeRequestContext) -> GoalSnapshotReply:
        snapshot = await Coordination.run_worker(
            partial(
                ctx.server.agent._comms.goals.goal_snapshot,
                ctx.name,
            )
        )
        turn = await Coordination.run_worker(
            partial(
                ctx.server.agent.turns.turn_state,
                ctx.session,
            )
        )
        return GoalSnapshotReply.from_snapshot(snapshot, UpdateMetadata.of(TurnChangedUpdate(turn)))


class ServedTranscriptPageRead(CapturesTranscriptPage, ThreadReadTool, TranscriptPageRead):
    """Serving one transcript page under the reader's authority."""

    def capture_as(self, comms: Comms, reader: ReadAuthority) -> TranscriptRead:
        return comms.transcripts.capture_page_read(
            reader.subject(self.thread),
            position=self.position,
            limit=self.limit,
            page_source=self.page_source,
        )


class ServedTeammateRead(UiRead, ReadTool, NativeToolRequest, TeammateRead):
    """Serving one teammate card, refused when the frame or the card moved on."""

    def apply(self, comms: Comms, reader: ReadAuthority) -> TeammateDetail:
        snapshot = comms.registry.snapshot()
        if not self.frame_owner.current(snapshot) or not self.unit.current(snapshot):
            raise ToolRefusedError(
                "The selected teammate or teammate incarnation is no longer current"
            )
        hud = TeamFrame.current(comms, self.frame_owner.name)
        hud.require_current()
        for card in hud.units:
            if card.unit == self.unit:
                return self.detail(comms, card)
        raise ToolRefusedError("The selected teammate is no longer in this teammate's published frame")

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
        details = tuple(TeamOrderDetail(
            order, (current[order.problem].order.checked().instructions(current[order.problem]),),
            OrderStory.collect(order.order, store.for_order(order.order)),
            tuple(ProgressSource.of(current[order.problem], comms.registry, pulls).commits()),
        ) for order in card.orders)
        turns = comms.transcripts.turns(card.unit.name, LastTurns())
        snapshot = comms.registry.snapshot()
        if turns.thread != card.unit or not card.unit.current(snapshot):
            raise ToolRefusedError("The selected incarnation changed while its details were read")
        return TeammateDetail(card, details, turns.to_wire())


@dataclass(frozen=True, kw_only=True)
class CommsOrderStoryTool(ReadTool, NativeToolRequest):
    label = "Order story"
    description = (
        "Read a saved order's dated facts from its Todo, oldest first. "
        "Send-back and its implied strike share one reason. "
        "Overwritten order bodies cannot be reconstructed."
    )
    order_id: Annotated[OrderId, *tool_field("Immutable order ID")]

    def apply(self, comms: Comms, caller: ToolCaller) -> OrderStory:
        return OrderStory.collect(self.order_id, comms.goals.todos.for_order(self.order_id))


@dataclass(frozen=True, kw_only=True)
class CommsTeamStoryTool(ReadTool, NativeToolRequest, declared_name="comms_fleet_story"):
    label = "Team story"
    description = ("Read what each held order on the shared board is doing now, one line per "
                   "order; orders held by unregistered participants collapse into one line.")

    def apply(self, comms: Comms, caller: ToolCaller) -> TeamStory:
        return TeamStory.collect(HeldOrders.read(comms))


@dataclass(frozen=True, kw_only=True)
class CommsOrderWorkTool(ReadTool, NativeToolRequest):
    label = "Order work"
    description = (
        "Read every tool call the problem's assignee made since its order, newest "
        "first, from the teammate's own turn records."
    )
    problem: Annotated[str, *tool_field("Problem ID")]

    def apply(self, comms: Comms, caller: ToolCaller) -> OrderWork:
        todo = comms.goals.todos.get(self.problem)
        since = todo.order.at.recorded_at("An order")
        turns = comms.transcripts.turns(todo.held().owner, TurnsSince(since))
        return OrderWork.read(self.problem, turns.thread, since, turns.turns)


@dataclass(frozen=True)
class WaivedEdits:
    """One order's edits an emergency release took out of the unjudged count."""

    problem: ProblemNumber
    edits: tuple[ToolCallId, ...]


@dataclass(frozen=True)
class EmergencyRelease:
    """Whose chokehold lifted, which edits were waived, and which orders could not be read
    (their edits still count)."""

    released: tuple[ThreadIncarnation, ...]
    waived: tuple[WaivedEdits, ...]
    unreadable: tuple[UnavailableOrderEdits, ...]

    def text(self, puller: str) -> str:
        """The #comms line, derived from the records it reports."""
        held = f" ({', '.join(identity.name for identity in self.released)})" if self.released else ""
        unreadable = (" Unreadable, still counting: "
                      + ", ".join(str(order.problem) for order in self.unreadable)
                      if self.unreadable else "")
        return (f"Emergency release pulled by {puller}: every chokehold is lifted{held} and "
                f"{sum(len(order.edits) for order in self.waived)} unjudged edits are waived as "
                "of now. Nobody forks judges for the old pile. Waived is not approved; new edits "
                f"count again.{unreadable}")


@dataclass(frozen=True)
class EmergencyReleased(ToolResult):
    """The release and the #comms notice that reports it, which the lifted goals resume on."""

    notice: MessageId
    release: EmergencyRelease


@dataclass(frozen=True, kw_only=True)
class CommsEmergencyReleaseTool(ThreadToolRequest, declared_name="comms_emergency_release"):
    """The andon cord: any teammate may pull it, the way anyone on a Toyota line can stop the
    line. The rules stay; only the current pile and the current holds go, and the pull is
    posted under the puller's name, so a cheat is caught in the open."""

    label = "Emergency release"
    description = (
        "The team's emergency stop, open to every teammate: lift every chokehold and waive every "
        "unjudged edit as of now. Posts the pull to #comms under your name; waived edits are "
        "neither approved nor struck, and new edits and vibe checks count again afterwards.")

    def apply(self, comms: Comms, caller: ThreadCaller) -> EmergencyReleased:
        actor = caller.identity(comms)
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
            waived.append(WaivedEdits(ProblemNumber.parse(read.todo.id),
                                      tuple(edit.id for edit in edits)))
            verdicts.extend(JudgedCall(read.todo.id, WaivedVerdict(
                read.todo.revision, actor, read.todo.held(), edit.id)) for edit in edits)
        if verdicts:
            comms.goals.judge_calls(tuple(verdicts), actor)
        held = tuple(thread.incarnation for thread in comms.registry.snapshot().threads.values()
                     if any(goal.state.lifts_on_compliance for goal in thread.goal))
        release = EmergencyRelease(held, tuple(waived), tuple(unreadable))
        notice = comms.messaging.send(caller.name, CommsTag.channel(), release.text(caller.name)).message
        for identity in held:
            comms.goals.update_goal(identity.name, CompliedGoalAction(witness=notice.message_id),
                                    actor=OwnerInvocable)
        return EmergencyReleased(notice.message_id, release)
