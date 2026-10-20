"""Compare current commander wording checks with their real native text owners."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from contextlib import redirect_stderr
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import ClassVar

from ..commander.config import CommanderConfig
from ..commander.order import CommanderContext, refuse
from ..comms import Comms
from ..errors import RelationViolationError, ToolRefusedError
from ..messages import AlertKind, Message
from ..task_sources import Decision
from ..todos import OrderText, OrderWords, PlainText, Todo, WordingCorrection
from ..tools import CommsDecisionTool, CommsProblemCloseTool
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


def decisions(source: Iterable[Message]) -> Iterable[tuple[Message, Decision]]:
    for message in source:
        try:
            decision = message.task.require_decision()
        except RelationViolationError:
            continue  # This actual message carries a different task capability.
        yield message, decision


@dataclass(frozen=True)
class WordingComparison(ParityComparison):
    observations: tuple[tuple[WordingOutcome, WordingOutcome], ...]
    owner: str
    native_owner: ClassVar[type[OrderWords]]

    @property
    def passed(self) -> bool:
        return bool(self.observations) and all(a == b for a, b in self.observations)

    def describe(self) -> str:
        return (
            f"{len(self.observations)} retained texts through commander.order.refuse and "
            f"{self.owner}; compared refusal and actual wording warnings; no publication"
        )

    @classmethod
    def compare_texts(cls, texts: Iterable[str]) -> ParityComparison:
        observed = tuple(
            (
                WordingOutcome.read(lambda: refuse(text, cls.native_owner)),
                WordingOutcome.read(lambda: cls.native_owner(text)),
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

    @classmethod
    def read(cls) -> ParityComparison:
        return cls.compare_texts(
            text
            for todo in history(selected_context())
            if todo.pending
            for text in (*todo.order.checked().edits, todo.order.checked().report)
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
            observed.append(WordingOutcome.read(lambda: OrderText(text)))
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
                lambda: AlertKind.require_publishable(message, Path(sender.worktree))
            )
            allowed += outcome.accepted
        rendered = matched = 0
        for message, decision in decisions(source):
            tool = CommsDecisionTool(
                target=message.target, chosen=decision.chosen, rejected=decision.rejected
            )
            rendered += 1
            matched += tool.original_body(decision) == message.body
        return UnverifiedParity(
            "Missing native decision-only alert policy: "
            f"{AlertKind.require_publishable.__func__.__qualname__} allowed "
            f"{allowed}/{len(alerts)} actual alerts. CommsDecisionTool rendered "
            f"{rendered} actual authored decisions, {matched} matching original bodies; "
            "that separate tool does not restrict AlertKind publication. "
            "No alert, decision or input was published or replayed."
        )
