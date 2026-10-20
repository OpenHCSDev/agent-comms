"""Observe wording checks at the current and native entry points without publishing."""

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
from ..errors import ToolRefusedError
from ..field_codec import FieldCodec
from ..goal_actions import GoalActionContext, OrderGoalAction
from ..messages import AlertKind, Message
from ..todos import (
    OrderText,
    OrderWords,
    PendingProblemOrder,
    PlainText,
    Todo,
)
from ..tool_result import ToolResult
from ..tools import CommsCorrectWordingTool, MessagePublished
from . import ParityComparison, UnverifiedParity
from .lifecycle import InstalledEvidenceCase, LifecycleObservation, LifecycleRun


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
        except (SystemExit, ToolRefusedError, ValueError):
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
                context.comms.goals, context.comms.registry.require(todo.held().owner),
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
                    todo.order.checked().copy_as(PendingProblemOrder), message.body, todo.text,
                ).problems_found(clean=True)
            current = WordingOutcome(not problems, output.getvalue())
            native = WordingOutcome.observe(partial(
                cls.native_request, context, todo, message.body,
            ))
            observed.append((current, native))
        return cls(tuple(observed)) if observed else UnverifiedParity(
            "No actual soft-worded message exists for the native request check."
        )


class ApplicationRunComparison(InstalledEvidenceCase):
    """Reuse the current close command and its real native return on the copied bus."""

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        delivered, closed = super().exercise(run)
        unquoted = replace(run, artifact=run.artifact.with_name("unquoted-goal.json"))
        created = unquoted.create()
        before = unquoted.complete(created)
        # This is the actual copied goal artifact, not an invented closing answer.
        WordingOutcome.observe(partial(
            unquoted.commander.close, before.id,
            unquoted.artifact.read_text(), str(unquoted.artifact),
        ))
        return delivered, closed, before, unquoted.commander.comms.goals.todos.get(before.id)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str,
    ) -> tuple[tuple[str, bool], ...]:
        return (
            *super().checks(run, rows[:2], native, output),
            ("application run declared", not rows[0].order.checked().run.mechanical),
            ("unquoted evidence refused without changing the order",
             rows[2] == rows[3] and rows[3].state.open),
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
        with LifecycleRun.open(extra_threads=(unit,)) as opened:
            run = replace(opened, worker=unit)
            before = run.create()
            request = CommsCorrectWordingTool(
                unit=unit, message_id=original.message_id, text=original.body,
            )
            result = FieldCodec.decode(
                MessagePublished, FieldCodec.encode(run.commander.call(request)),
            )
            after = run.commander.comms.goals.todos.get(before.id)
            with run.commander.comms.bus.log.opened_wire_snapshot() as source:
                (published,) = source.messages_for_references((result.message.source,))
            return LifecycleObservation(
                owner=CommsCorrectWordingTool.apply.__qualname__,
                invocations=sum(item.request is request for item in run.commander.invocations),
                checks=(
                    ("native correction returned its actual wire message",
                     published.message_id == result.id and result.message.matches(published)),
                    ("correction addressed its unit", published.target == unit),
                    ("correction names the actual message", original.message_id in published.body),
                    ("correction names the open order", before.id in published.body),
                    ("wording never adds a strike", before.strikes == after.strikes),
                    ("wording leaves the order unchanged", before == after),
                ),
                output=published.body,
            )


class DecisionAlertsComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = selected_context()
        registry = context.comms.registry.snapshot()
        observed = []
        for message in messages(context):
            sender = registry.canonical_name(message.sender)
            if message.type is AlertKind and sender in registry.threads:
                owner = registry.threads[sender]
                observed.append(WordingOutcome.observe(partial(
                    AlertKind.require_publishable, message, Path(owner.worktree),
                )))
        return UnverifiedParity(
            "Missing native owner: AlertKind has no decision-only publication policy; "
            f"it inherits {AlertKind.require_publishable.__qualname__}. "
            f"Called that real validator on {len(observed)} actual alerts with live senders; "
            f"allowed={sum(result.accepted for result in observed)}. "
            "CommsDecisionTool records authored choices separately, not alert permission. "
            "No alert, decision or input was published."
        )
