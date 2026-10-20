"""Compare current commander wording checks with their real native text owners."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from contextlib import redirect_stderr
from dataclasses import dataclass, replace
from functools import partial
from itertools import islice
from pathlib import Path
from typing import ClassVar

from ..commander.config import CommanderConfig
from ..commander.order import CommanderContext, OrderFile, refuse
from ..comms import Comms
from ..errors import ToolRefusedError
from ..goal_actions import GoalActionContext, OrderGoalAction
from ..message_reference import MessageReference
from ..messages import AlertKind, Message
from ..todos import (
    OrderText,
    OrderWords,
    PendingProblemOrder,
    PlainText,
    Todo,
    WordingCorrection,
)
from ..tools import CommsProblemCloseTool
from . import ParityComparison, UnverifiedParity


@dataclass(frozen=True)
class WordingOutcome:
    accepted: bool
    notes: str

    @classmethod
    def read(cls, operation: Callable[[], object]) -> WordingOutcome:
        notes = io.StringIO()
        try:
            with redirect_stderr(notes):
                operation()
        except SystemExit, ToolRefusedError:
            return cls(False, notes.getvalue())
        return cls(True, notes.getvalue())


def selected_context() -> CommanderContext:
    return CommanderContext.read(CommanderConfig.path())


def history(context: CommanderContext) -> tuple[Todo, ...]:
    return context.comms.goals.todos.history_for_commander(context.incarnation)


def messages(comms: Comms) -> tuple[Message, ...]:
    with comms.bus.log.opened_wire_snapshot() as source:
        return tuple(islice(source.newest_messages(), 2000))


@dataclass(frozen=True)
class WordingComparison(ParityComparison):
    observations: tuple[tuple[WordingOutcome, WordingOutcome], ...]
    owner: str
    references: tuple[MessageReference, ...] = ()
    native_owner: ClassVar[type[OrderWords]]
    current_owner: ClassVar[Callable[..., object]] = refuse

    @property
    def passed(self) -> bool:
        return bool(self.observations) and all(a == b for a, b in self.observations)

    def describe(self) -> str:
        different = sum(a != b for a, b in self.observations)
        return (
            f"{len(self.observations)} retained texts; mismatches={different}; "
            f"current_refusals={sum(not a.accepted for a, _ in self.observations)}; "
            f"native_refusals={sum(not b.accepted for _, b in self.observations)}; "
            f"current={self.current_owner.__module__}.{self.current_owner.__qualname__}; "
            f"native={self.owner}; sources={self.references}; "
            "compared refusal and warnings; no publication"
        )

    @classmethod
    def compare_texts(cls, texts: Iterable[str]) -> ParityComparison:
        observed = tuple(
            (
                WordingOutcome.read(partial(refuse, text, cls.native_owner)),
                WordingOutcome.read(partial(cls.native_owner, text)),
            )
            for text in texts
        )
        if not observed:
            return UnverifiedParity("No retained text exists for this wording comparison.")
        return cls(observed, f"{cls.native_owner.__module__}.{cls.native_owner.__qualname__}")


class PlainWordsComparison(WordingComparison):
    native_owner = PlainText

    @classmethod
    def read(cls) -> ParityComparison:
        return cls.compare_texts(
            strike.reason for todo in history(selected_context()) for strike in todo.strikes
        )


class OrderWordingComparison(WordingComparison):
    native_owner = OrderText
    current_owner = OrderFile.problems_found

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
                context.comms.goals, context.comms.registry.require(todo.held().owner)
            )
        )

    @classmethod
    def read(cls) -> ParityComparison:
        context = selected_context()
        return cls.compare_messages(context, messages(context.comms))

    @classmethod
    def compare_messages(
        cls, context: CommanderContext, source: Iterable[Message]
    ) -> ParityComparison:
        held = tuple(todo for todo in history(context) if todo.pending and todo.holder)
        if not held:
            return UnverifiedParity("No held order exists for the native pre-reservation check.")
        todo = max(held, key=lambda item: item.order.checked().at.recorded_at("A live order"))
        cases = tuple(message for message in source if cls.native_owner.refused(message.body))
        if not cases:
            return UnverifiedParity(
                "No actual soft-worded message exists for a refusal comparison."
            )
        observed = []
        for message in cases:
            text = message.body
            notes = io.StringIO()
            with redirect_stderr(notes):
                refused = OrderFile(
                    todo.order.checked().copy_as(PendingProblemOrder), text, todo.text
                ).problems_found(clean=True)
            current = WordingOutcome(not refused, notes.getvalue())
            native = WordingOutcome.read(partial(cls.native_request, context, todo, text))
            observed.append((current, native))
        return cls(
            tuple(observed),
            "native ProblemOrderGoalAction.request_ref (no reservation/write); "
            "current OrderFile.problems_found; OrderText selects actual refusal cases",
            tuple(message.reference for message in cases),
        )


class ApplicationRunComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = selected_context()
        observed = []
        for todo in history(context):
            if not todo.pending:
                continue
            order = todo.order.checked()
            if order.run.mechanical:
                continue
            text = "\n".join(order.run.instructions())
            observed.append(WordingOutcome.read(partial(OrderText, text)))
            # This is the real native request owner, not a substituted store or caller.
            # Construct its operation only: no artifact or successful run is invented.
            CommsProblemCloseTool(problem=todo.id, works=text, artifact="").operation_for(
                todo, context.incarnation, context.comms
            )
        return UnverifiedParity(
            "Missing current owner: scratch rts/order.py send (Works when:) is not "
            f"available on main. Called native OrderText and CommsProblemCloseTool "
            f"on {len(observed)} actual application-order declarations without closing "
            "anything. OrderText itself has no required-run policy; that behavior "
            "belongs to ApplicationOrderRun, so the row's native description is stale."
        )


class UnitWordingComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = selected_context()
        held = tuple(todo for todo in history(context) if todo.pending)
        compared = changed_strikes = 0
        for message in messages(context.comms):
            words = tuple(PlainText.found(message.body))
            for todo in held:
                if todo.holder == message.sender and words:
                    corrected = WordingCorrection(todo.revision, message.message_id, words).apply(
                        todo
                    )
                    compared += 1
                    changed_strikes += corrected.strikes != todo.strikes
        return UnverifiedParity(
            "Missing current owner: scratch vibe_watch.py is not available on main. "
            f"Native PlainText and WordingCorrection examined {compared} live unit/order "
            f"pairs; changed strike lists={changed_strikes}. No correction was published "
            "or stored; no current/native workflow equivalence is claimed."
        )


class DecisionAlertsComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = selected_context()
        source = messages(context.comms)
        alerts = tuple(message for message in source if message.type is AlertKind)
        allowed = 0
        for message in alerts:
            sender = context.comms.registry.require(message.sender)
            outcome = WordingOutcome.read(
                partial(AlertKind.require_publishable, message, Path(sender.worktree))
            )
            allowed += outcome.accepted
        return UnverifiedParity(
            "Missing native decision-only alert policy: "
            f"{AlertKind.require_publishable.__qualname__} allowed "
            f"{allowed}/{len(alerts)} actual alerts. CommsDecisionTool declares authored "
            "choices separately; that does not restrict AlertKind publication. "
            "No alert, decision or input was published or replayed."
        )
