"""Compare wording and authored messages through real owners and disposable buses."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Callable, Iterable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from functools import partial
from itertools import islice
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import CommanderContext, OrderFile, refuse
from agent_comms.commander_parity import ParityComparison, UnverifiedParity
from agent_comms.commander_parity.lifecycle import (
    InstalledEvidenceCase,
    LifecycleObservation,
    LifecycleRun,
)
from agent_comms.comms import Comms
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import GoalActionContext, OrderGoalAction
from agent_comms.messages import AlertKind, InfoKind, Message, NoticeRole
from agent_comms.task_sources import ExplicitTaskScopeSelection, NoTaskAttachment, ProjectTaskScope
from agent_comms.todos import (
    OrderText,
    OrderWords,
    PendingProblemOrder,
    PlainText,
    Todo,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tool_result import ToolResult
from agent_comms.tools import (
    CommsCorrectWordingTool,
    CommsDecisionTool,
    MessagePublished,
    TaskPublished,
)


@dataclass(frozen=True)
class WordingOutcome:
    accepted: bool
    warnings: str

    @classmethod
    def observe(cls, operation: Callable[[], object]) -> WordingOutcome:
        output = io.StringIO()
        try:
            with redirect_stderr(output):
                operation()
        except SystemExit, ToolRefusedError:
            return cls(False, output.getvalue())
        return cls(True, output.getvalue())


def selected_context() -> CommanderContext:
    return CommanderContext.read(CommanderConfig.path())


def history(context: CommanderContext) -> tuple[Todo, ...]:
    return context.comms.goals.todos.history_for_commander(context.incarnation)


def messages(context: CommanderContext) -> tuple[Message, ...]:
    with context.comms.bus.log.opened_wire_snapshot() as source:
        return tuple(islice(source.newest_messages(), 2000))


@dataclass(frozen=True)
class WordingComparison(ParityComparison):
    observations: tuple[tuple[WordingOutcome, WordingOutcome], ...]
    native_owner: ClassVar[type[OrderWords]]

    @staticmethod
    def agrees(current: WordingOutcome, native: WordingOutcome) -> bool:
        return current == native

    @property
    def passed(self) -> bool:
        return bool(self.observations) and all(self.agrees(a, b) for a, b in self.observations)

    def describe(self) -> str:
        return (
            f"observed={len(self.observations)}; "
            f"mismatches={sum(not self.agrees(a, b) for a, b in self.observations)}; "
            f"current_refusals={sum(not a.accepted for a, _ in self.observations)}; "
            f"native_refusals={sum(not b.accepted for _, b in self.observations)}; "
            f"native={self.native_owner.__module__}.{self.native_owner.__qualname__}; "
            "compared actual refusal and warning results; no publication"
        )

    @classmethod
    def compare_texts(cls, texts: Iterable[str]) -> ParityComparison:
        observed = tuple(
            (
                WordingOutcome.observe(partial(refuse, text, cls.native_owner)),
                WordingOutcome.observe(partial(cls.native_owner, text)),
            )
            for text in texts
        )
        return cls(observed) if observed else UnverifiedParity("No actual wording input exists.")


class PlainWordsComparison(WordingComparison):
    native_owner = PlainText

    @classmethod
    def read(cls) -> ParityComparison:
        return cls.compare_texts(
            strike.reason for todo in history(selected_context()) for strike in todo.strikes
        )


class OrderWordingComparison(WordingComparison):
    native_owner = OrderText

    @staticmethod
    def agrees(current: WordingOutcome, native: WordingOutcome) -> bool:
        # This row asks whether soft orders are refused, not whether surrounding
        # CLI prose emits the same unrelated wording notes as the native request.
        return current.accepted == native.accepted

    def describe(self) -> str:
        return super().describe() + (
            f"; current={OrderFile.problems_found.__qualname__}; "
            f"native_entry={OrderGoalAction.request_ref.__qualname__}"
        )

    @staticmethod
    def native_request(context: CommanderContext, todo: Todo, text: str) -> object:
        order = replace(todo.order.checked().copy_as(PendingProblemOrder), edits=(text,))
        action = OrderGoalAction(
            problem=todo.id,
            expected_revision=todo.revision,
            order=order,
            request=context.comms.registry.require(context.commander).goal.require().id,
            deadline=todo.deadline,
            commander=context.commander,
            assigner=context.incarnation,
        )
        return action.request_ref(
            GoalActionContext(
                context.comms.goals,
                context.comms.registry.require(todo.held().owner),
            )
        )

    @classmethod
    def read(cls) -> ParityComparison:
        context = selected_context()
        held = tuple(todo for todo in history(context) if todo.pending and todo.holder)
        if not held:
            return UnverifiedParity("No actual held order exists for the native request check.")
        todo = max(held, key=lambda item: item.order.checked().at.recorded_at("An order"))
        observed = []
        for message in messages(context):
            if not cls.native_owner.refused(message.body):
                continue
            output = io.StringIO()
            with redirect_stderr(output):
                problems = OrderFile(
                    todo.order.checked().copy_as(PendingProblemOrder),
                    message.body,
                    todo.text,
                ).problems_found(clean=True)
            current = WordingOutcome(not problems, output.getvalue())
            native = WordingOutcome.observe(
                partial(
                    cls.native_request,
                    context,
                    todo,
                    message.body,
                )
            )
            observed.append((current, native))
        return (
            cls(tuple(observed))
            if observed
            else UnverifiedParity(
                "No actual soft-worded message exists for the native request check."
            )
        )


class ApplicationRunComparison(InstalledEvidenceCase):
    """Reuse the current close command and its real native return on the copied bus."""

    @classmethod
    def before_close(cls, run: LifecycleRun, delivered: Todo) -> tuple[Todo, ...]:
        # Try the actual goal JSON before the valid closing of this same order.
        WordingOutcome.observe(
            partial(
                run.commander.close,
                delivered.id,
                run.artifact.read_text(),
                str(run.artifact),
            )
        )
        return delivered, run.commander.comms.goals.todos.get(delivered.id)

    @classmethod
    def checks(
        cls,
        run: LifecycleRun,
        rows: tuple[Todo, ...],
        native: tuple[ToolResult, ...],
        output: str,
    ) -> tuple[tuple[str, bool], ...]:
        return (
            *super().checks(run, rows[:2], native, output),
            ("application run declared", not rows[0].order.checked().run.mechanical),
            (
                "unquoted evidence refused without changing the order",
                rows[2] == rows[3] and rows[3].state.open,
            ),
        )


class UnitWordingComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = selected_context()
        registry = context.comms.registry.snapshot()
        cases = tuple(
            (message, registry.canonical_name(message.sender))
            for message in messages(context)
            if registry.canonical_name(message.sender) in registry.threads
            and registry.canonical_name(message.sender) != context.commander
            and PlainText.found(message.body)
        )
        if not cases:
            return UnverifiedParity("No actual live unit message contains wording to correct.")
        original, unit = cases[0]
        with LifecycleRun.open(extra_threads=(unit,)) as opened, redirect_stdout(io.StringIO()):
            run = replace(opened, worker=unit)
            before = run.create()
            request = CommsCorrectWordingTool(
                unit=unit,
                message_id=original.message_id,
                text=original.body,
            )
            result = FieldCodec.decode(
                MessagePublished,
                FieldCodec.encode(run.commander.call(request)),
            )
            after = run.commander.comms.goals.todos.get(before.id)
            with run.commander.comms.bus.log.opened_wire_snapshot() as source:
                (published,) = source.messages_for_references((result.message.source,))
            return LifecycleObservation(
                owner=CommsCorrectWordingTool.apply.__qualname__,
                invocations=sum(item.request is request for item in run.commander.invocations),
                checks=(
                    (
                        "native correction returned its actual wire message",
                        published.message_id == result.id and result.message.matches(published),
                    ),
                    ("correction addressed its unit", published.target == unit),
                    ("correction names the actual message", original.message_id in published.body),
                    ("correction names the open order", before.id in published.body),
                    ("wording never adds a strike", before.strikes == after.strikes),
                    ("wording leaves the order unchanged", before == after),
                ),
                output=published.body,
            )


class DecisionAlertsComparison:
    @staticmethod
    def publish(root: str, sender: str, target: str) -> None:
        # This child owns a real process and registry turn on the copied bus.
        # The tool derives authorship from that lease, never constructed turn fields.
        comms = Comms(Path(root))
        owner = comms.registry.require(sender)
        comms.registry.register(
            replace(owner, process_identity=ProcessIdentity.capture(os.getpid())), new_owner=True
        )
        leased, _ = comms.registry.lease_local_turn(sender, uuid4().hex)
        lease = leased.require_turn_lease()
        scope = ProjectTaskScope(project=leased.worktree)
        try:
            request = CommsDecisionTool(
                target=target,
                scope=ExplicitTaskScopeSelection(scope),
                chosen="Keep alerts open.",
                rejected=("Require every alert to carry a declared choice.",),
            )
            result = FieldCodec.decode(
                TaskPublished,
                CommsDecisionTool.invoke(comms, request.arguments, caller=ThreadCaller(sender)),
            )
            with comms.bus.log.opened_wire_snapshot() as source:
                (decision_message,) = source.messages_for_references((result.reference,))
            decision = decision_message.require_decision()
            with comms.bus.log.checked_read() as checked:
                decision_message.require_task_publication(sender, comms.registry.snapshot(), checked)
            alert = comms.messaging.send(
                sender,
                target,
                "Copied-bus failure notice: alerts stay open.",
                AlertKind,
                role=NoticeRole(),
            ).message
            with comms.bus.log.opened_wire_snapshot() as source:
                saved = source.messages_for_references((result.reference, alert.reference))
            observation = LifecycleObservation(
                owner=(
                    f"{CommsDecisionTool.apply.__qualname__}; "
                    f"{Message.require_decision.__qualname__}; "
                    f"{Message.require_task_publication.__qualname__}; "
                    f"{AlertKind.require_publishable.__qualname__}({AlertKind.__qualname__})"
                ),
                invocations=len(saved),
                checks=(
                    ("decision tool returns its published task", result.task == decision),
                    ("decision keeps its actual sender", decision.author == leased.incarnation),
                    (
                        "decision keeps its original turn",
                        decision.source_turn == lease.identity
                        and decision.source_turn_id.value == lease.turn_id,
                    ),
                    ("decision keeps its declared project", decision.scope == scope),
                    (
                        "authored decision remains an info notice",
                        decision_message.type is InfoKind
                        and decision_message.role == NoticeRole(),
                    ),
                    (
                        "bare alert publishes without a decision",
                        alert.type is AlertKind and alert.task == NoTaskAttachment(),
                    ),
                    (
                        "failure notice never starts a turn",
                        not alert.role.response_policy(alert).starts_turn,
                    ),
                    ("both actual wire messages retained", saved == (decision_message, alert)),
                ),
                output=f"decision={result.reference}; bare alert={alert.reference}",
            )
        finally:
            comms.registry.release_turn(lease)
        print(json.dumps(FieldCodec.encode(observation)))

    @classmethod
    def read(cls) -> ParityComparison:
        with LifecycleRun.open() as run:
            child = run.invoke(
                "import sys; from agent_comms.commander_parity.wording import "
                "DecisionAlertsComparison; DecisionAlertsComparison.publish(*sys.argv[1:])",
                str(run.commander.root),
                run.worker,
                run.commander.commander,
            )
            if not child.outcome.successful:
                raise RuntimeError(
                    f"Copied decision publication {child.outcome.summary}: "
                    f"{child.stderr.decode(errors='replace')}"
                )
            return FieldCodec.decode(LifecycleObservation, FieldCodec.parse(child.stdout.decode()))
