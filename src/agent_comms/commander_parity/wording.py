"""Observe wording checks at the current and native entry points without publishing."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from contextlib import redirect_stderr
from dataclasses import dataclass, replace
from functools import partial
from itertools import islice
from typing import ClassVar

from ..commander.config import CommanderConfig
from ..commander.order import CommanderContext, OrderFile, refuse
from ..errors import ToolRefusedError
from ..goal_actions import GoalActionContext, OrderGoalAction
from ..messages import Message
from ..todos import OrderText, OrderWords, PendingProblemOrder, PlainText, Todo
from . import ParityComparison, UnverifiedParity


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
        except (SystemExit, ToolRefusedError):
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

    @property
    def passed(self) -> bool:
        return bool(self.observations) and all(a == b for a, b in self.observations)

    def describe(self) -> str:
        return (
            f"observed={len(self.observations)}; "
            f"mismatches={sum(a != b for a, b in self.observations)}; "
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
            GoalActionContext(context.comms.goals, context.comms.registry.require(todo.held().owner))
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
            native = WordingOutcome.observe(partial(cls.native_request, context, todo, message.body))
            observed.append((current, native))
        return cls(tuple(observed)) if observed else UnverifiedParity(
            "No actual soft-worded message exists for the native request check."
        )
