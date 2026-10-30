"""Todo and order tools, with the result records they return."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Annotated, Literal

from agent_comms_hooks import ToolRefusedError
from pydantic import JsonValue

from agent_comms.todos import (
    ClaimOrder,
    CorrectCheckTodo,
    CorrectRunTodo,
    DeliveryReport,
    InstallTodo,
    OrderClaim,
    OrderCompletion,
    OrderProgress,
    TodoProblems,
    PlainText,
    ProgressRead,
    PullRequestLink,
    ResumeClaimOrder,
    HeadReviewed,
    HeadVerdict,
    NoHeadOrder,
    ServeTodo,
    StrikeReason,
    Todo,
    TodoOperation,
    JudgedCall,
    ToolCallVerdict,
)
from .commander_hud import CommanderHud, CommanderLoad
from .service_passes import ServicePassStore
from .comms import Comms
from .field_codec import FieldCodec, MessageId, OrderId, RecordProblems, WireName
from .problem_number import ProblemNumber
from .tool_declaration import EditCall
from .work_branches import WorkBranchError
from .goal_actions import CreateProblemGoalAction, OrderGoalAction
from .goals import Goal, OrderCheck
from .head_review import HeadReviewOrder, PreparedHeadReview, PullRequestHead
from .held_orders import HeldOrders
from .input_disposition import DeliveryOverview
from .order_story import FleetStory, OrderStory
from .order_work import OrderWork
from .plan_phases import OrderPhase
from .progress_claims import ProgressClaim
from .send_result import CorrectionResult, SendResult
from .thread_identity import ThreadIncarnation
from .tool_caller import ThreadCaller, ToolCaller
from .tool_request import (
    PublishingToolRequest,
    ReadTool,
    ThreadReadTool,
    ThreadToolRequest,
    ToolRequest,
    tool_field,
)
from .tool_result import ToolResult
from .transcript_events import SentTranscript
from .turn_digests import (
    ActivityTurnView,
    LastTurns,
    OrderEditsRead,
    ThreadTurns,
    TurnBound,
    TurnView,
    TurnsSince,
    UnjudgedEdits,
    UnavailableOrderEdits,
)


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
class MessagePublished(ToolResult):
    """The Pi adapter decodes this reply as PublishedMessageToolDetails by its agentCommsKind."""

    id: MessageId
    message: SentTranscript
    correction: CorrectionResult
    agent_comms_kind: Annotated[
        Literal["published_message"], WireName("agentCommsKind")
    ] = "published_message"

    @classmethod
    def from_send(cls, result: SendResult) -> MessagePublished:
        return cls(result.message.message_id, SentTranscript.from_message(result.message), result.correction)


@dataclass(frozen=True, kw_only=True)
class CommsTodoTool(ThreadToolRequest):
    label = "Change problem"
    description = (
        "Apply an exact revision-checked Todo command; only its commander may invoke this tool."
    )
    problem: Annotated[str, *tool_field("Problem ID")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoChanged | ProblemClosed:
        todo = comms.goals.todos.get(self.problem)
        actor = caller.identity(comms)
        return self.result(comms.goals.change_problem(
            self.problem, self.operation_for(todo, actor, comms), caller.commander(comms).name))

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
        "Record the commander's verdict on one tool call the holder made for its "
        "order: an accepted verdict quotes 4+ words of the order; a send-back strikes "
        "the holder and revises its Goal in the same write."
    )
    problem: Annotated[str, *tool_field("Problem ID")]
    operation: Annotated[
        ToolCallVerdict, *tool_field("Accepted or send-back verdict on the judged tool call ID")
    ]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoChanged:
        changed, = comms.goals.judge_calls(
            (JudgedCall(self.problem, self.operation.authored_by(caller.identity(comms))),),
            caller.commander(comms).name)
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
            caller.commander(comms).name))


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
        problems: dict[str, str] = {}
        orders = self.order.prepared(comms.goals, problems)
        actors = []
        for order in orders:
            with RecordProblems.collecting(problems, '--problem', 'select the current held order'):
                actors.append(caller.head_review_actor(comms, self.request, order))
        heads = []
        with RecordProblems.collecting(problems, '--head', 'supply the current PR head SHA with --head'):
            heads.append(self.request.current())
        preparations = []
        for current in heads:
            with RecordProblems.collecting(problems, self.operation.preparation_field,
                                          self.operation.preparation_fix):
                preparations.append(PreparedHeadReview(current, self.operation.subject(comms.goals, current)))
        if problems:
            raise TodoProblems(problems)
        order, = orders
        actor, = actors
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
        for read in OrderEditsRead.for_commander(comms, owner.name):
            try:
                edits.extend(read.read())
            except (WorkBranchError, OSError) as error:
                unavailable.append(UnavailableOrderEdits(
                    ProblemNumber.parse(read.todo.id), read.thread.incarnation, error))
        return UnjudgedEdits(tuple(edits), tuple(unavailable)).with_services(passes, owner)


@dataclass(frozen=True, kw_only=True)
class BoardReadTool(ThreadReadTool, declared_name=None):
    """A read of one commander's board. An omitted thread reads the caller's own, and an
    empty board looks like a broken reader, so the reply names whose board it read."""

    def reply(self, read: dict[str, JsonValue], caller: ToolCaller) -> dict[str, JsonValue]:
        return {"board": caller.subject(self.thread), **read}


@dataclass(frozen=True, kw_only=True)
class CommsCommanderLoadTool(BoardReadTool):
    label = "Measured commander load"
    description = "Read retained commander load with recorded service failures and its original completion clock."

    def apply(self, comms: Comms, caller: ToolCaller) -> CommanderLoad:
        return CommanderHud.read(comms, caller.subject(self.thread)).load_reading()


@dataclass(frozen=True, kw_only=True)
class CommsHudTool(ThreadReadTool):
    label = "Commander HUD"
    description = (
        "Read the commander's latest published Tristan frame: orders, review timers, "
        "strikes, edits, PRs, plan phases and load. Recorded failures precede retained data; "
        "stale rows stay explicitly stale, and missing frames remain unavailable."
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> CommanderHud:
        return CommanderHud.read(comms, caller.subject(self.thread))


@dataclass(frozen=True, kw_only=True)
class CommsFireTool(ThreadToolRequest):
    label = "Fire unit"
    description = (
        "Release every open problem the unit holds in one operation, recording one "
        "plain reason on each; only their commander may invoke this tool."
    )
    unit: Annotated[str, *tool_field("Unit thread to fire")]
    reason: Annotated[StrikeReason, *tool_field("What the unit did, plainly")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoList:
        return TodoList(comms.goals.fire(self.unit, self.reason, caller.identity(comms)))


@dataclass(frozen=True, kw_only=True)
class CommsCorrectWordingTool(ThreadToolRequest):
    label = "Correct unit wording"
    description = (
        "Answer a unit's jargon with a correction naming its open orders; the unit's "
        "strikes stay as they are. Only its commander may invoke this tool."
    )
    unit: Annotated[str, *tool_field("Unit thread whose message used jargon")]
    message_id: Annotated[MessageId, *tool_field("The unit's message")]
    text: Annotated[str, *tool_field("The unit's message text")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> MessagePublished:
        actor = comms.registry.require(caller.name).incarnation
        orders = [todo.id for todo in comms.goals.todos.for_commander(actor)
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
    description = "Read the commander's canonical Todo orders, review timers, strikes and hunts."

    def apply(self, comms: Comms, caller: ToolCaller) -> TodoList:
        snapshot = comms.registry.snapshot()
        actor = snapshot.require(caller.subject(self.thread)).incarnation
        listed = tuple(comms.goals.todos.for_commander(actor))
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
        "the commander's review timer; does not replay input."
    )
    order: Annotated[
        OrderGoalAction, *tool_field(
            "Problem, request, expected_revision, review_at, commander, and together: the "
            "order's other open problems with their expected revisions")
    ]
    owner: Annotated[str, *tool_field("Assignee thread")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProblemOrdered:
        goal = comms.goals.update_goal(self.owner, self.order.bound_to(
            caller.identity(comms), caller.commander(comms).incarnation))
        return ProblemOrdered(goal, tuple(comms.goals.todos.get(problem)
                                          for problem in self.order.problems()))


@dataclass(frozen=True, kw_only=True)
class CommsTodoCreateTool(CommsOrderTool):
    label = "Create problem"
    description = "Create a problem with its owner, checked order and review timer in one command."
    order: Annotated[
        CreateProblemGoalAction, *tool_field("Typed problem creation and order")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsOrderDeliverTool(PublishingToolRequest[ThreadCaller], ThreadToolRequest):
    label = "Claim order delivery"
    description = (
        "Record the holder's delivery claim and answer its commander. "
        "Only the holder may claim a pending order; this does not accept its delivery."
    )
    problem: Annotated[str, *tool_field("Held problem ID")]
    pull_request: Annotated[int, *tool_field("Pull request number in the problem's repository")]
    head: Annotated[str, *tool_field("Claimed head commit")]
    report: Annotated[DeliveryReport, *tool_field(
        "Delivery report: the slop you killed or the broken thing you fixed, with file:line, "
        "in the owner's voice"
    )]

    def publish(self, comms: Comms, caller: ThreadCaller) -> TodoChanged:
        todo = comms.goals.todos.get(self.problem)
        actor = comms.registry.require(caller.name).incarnation
        generation = todo.held().generation
        todo.require_assignee(actor, generation)
        # The identical call resumes a retained claim's publication; nothing is claimed again.
        link = PullRequestLink(todo.repo, self.pull_request)
        for claim in todo.order.commander_claims():
            if claim.resumed_by(link, self.head, self.report.text):
                resumed = ResumeClaimOrder(todo.revision, actor, generation, claim).record(
                    comms.goals, todo.id)
                return TodoChanged(resumed, resumed.strike_count)
        original = comms.bus.bus.lookup_message(todo.latest_message.message_id)
        claim = OrderClaim.for_order(
            todo, actor, self.pull_request, self.head, self.report.text,
            original.reference, store=comms.goals.todos,
        )
        changed = ClaimOrder(
            todo.revision, actor, generation, claim
        ).record(comms.goals, todo.id)
        return TodoChanged(changed, changed.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsOrderDoneTool(CommsTodoTool):
    label = "Check delivered order"
    description = "Verify and deliver an open order by its own ID; the unit's Goal is unchanged."
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
        return self.delivery.operation(todo, actor, comms, self.order_id, self.claims)


@dataclass(frozen=True, kw_only=True)
class CommsOrderCheckTool(CommsTodoTool):
    label = "Correct order check"
    description = (
        "Replace the check of an open order, recording the old check, the new one and "
        "why; only its commander may invoke this tool, and the order keeps its review timer "
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
        "and why; only its commander may invoke this tool, and the order keeps its review "
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
        "Name the plan phase an open order serves; only its commander may invoke this "
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


@dataclass(frozen=True, kw_only=True)
class CommsInputDeliveryTool(ThreadReadTool):
    label = "Input delivery"
    description = ("Read a unit's inputs that failed or are UNKNOWN, and its earlier delivery "
                   "notices, each with its bus sequence and input id; none is ever replayed.")

    def apply(self, comms: Comms, caller: ToolCaller) -> DeliveryOverview:
        return comms.goals.input_delivery(caller.subject(self.thread), include_history=True)


@dataclass(frozen=True, kw_only=True)
class CommsOrderStoryTool(ReadTool, ToolRequest):
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
class CommsFleetStoryTool(BoardReadTool):
    label = "Fleet story"
    description = ("Read what each order this commander holds out is doing now, one line per "
                   "order; orders held by units no longer registered collapse into one line.")

    def apply(self, comms: Comms, caller: ToolCaller) -> FleetStory:
        return FleetStory.collect(HeldOrders.read(comms, caller.subject(self.thread)))


@dataclass(frozen=True, kw_only=True)
class CommsOrderWorkTool(ReadTool, ToolRequest):
    label = "Order work"
    description = (
        "Read every tool call the problem's assignee made since its order, newest "
        "first, from the unit's own turn records."
    )
    problem: Annotated[str, *tool_field("Problem ID")]

    def apply(self, comms: Comms, caller: ToolCaller) -> OrderWork:
        todo = comms.goals.todos.get(self.problem)
        since = todo.order.at.recorded_at("An order")
        turns = comms.transcripts.turns(todo.held().owner, TurnsSince(since))
        return OrderWork.read(self.problem, turns.thread, since, turns.turns)
