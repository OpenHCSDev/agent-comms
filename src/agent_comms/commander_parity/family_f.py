"""Live configuration and transfer comparisons; absent workflows fail closed."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ..change_report import FileLine
from ..commander.config import CommanderConfig
from ..comms import Comms
from ..thread_identity import ThreadIncarnation
from ..todos import Assignment, Todo
from . import ParityComparison, UnverifiedParity


@dataclass(frozen=True)
class ConfigurationComparison(ParityComparison):
    current: CommanderConfig
    native: CommanderConfig
    registered: ThreadIncarnation
    project: Path

    @property
    def passed(self) -> bool:
        return (
            self.current == self.native
            and self.registered.name == self.current.commander
            and any(
                self.project.is_relative_to(repo.checkout.resolve()) for repo in self.native.repos
            )
        )

    def describe(self) -> str:
        return (
            f"home_config_matches_root_record={self.current == self.native}; "
            f"registered={self.registered.name}; project={self.project}; root={self.native.root}"
        )

    @classmethod
    def read(cls) -> ParityComparison:
        current = CommanderConfig.read(CommanderConfig.path())
        native = CommanderConfig.read_root(current.root)
        registered = Comms(native.root).registry.require(native.commander)
        return cls(current, native, registered.incarnation, registered.project_path)


@dataclass(frozen=True)
class TraceOutcome:
    refused: bool
    reason: str

    @classmethod
    def read(cls, action: Callable[[], object]) -> TraceOutcome:
        try:
            action()
        except ValueError as error:
            return cls(True, str(error))
        return cls(False, "")


@dataclass(frozen=True)
class TransferComparison(ParityComparison):
    problem: str
    current_without_trace: TraceOutcome
    native_without_trace: TraceOutcome
    current_with_trace: TraceOutcome
    native_with_trace: TraceOutcome
    unchanged: bool

    @property
    def passed(self) -> bool:
        return (
            self.current_without_trace == self.native_without_trace
            and self.current_without_trace.refused
            and self.current_with_trace == self.native_with_trace
            and not self.current_with_trace.refused
            and self.unchanged
        )

    def describe(self) -> str:
        return f"current=Assignment.require_trace; native=Todo.assigned; {self!r}"

    @classmethod
    def read(cls) -> ParityComparison:
        config = CommanderConfig.read(CommanderConfig.path())
        store = Comms(config.root).goals.todos
        with store.snapshot() as db:
            history = tuple(Todo.stream(db))
        for todo in history:
            assignment = todo.assignment
            if not todo.pending or assignment is None:
                continue
            proposed = next(
                (
                    other.assignment
                    for other in history
                    if other.assignment is not None
                    and other.assignment.owner != assignment.owner
                    and other.assignment.generation != assignment.generation
                ),
                None,
            )
            if proposed is None:
                continue
            current_without = TraceOutcome.read(
                partial(assignment.require_trace, proposed.owner, None)
            )
            if not current_without.refused:
                continue
            source = inspect.getsourcefile(Assignment.require_trace)
            if source is None:
                return UnverifiedParity(
                    "Assignment.require_trace has no inspectable source location"
                )
            trace = FileLine.from_wire(
                f"{source}:{inspect.getsourcelines(Assignment.require_trace)[1]}"
            )
            return cls(
                todo.id,
                current_without,
                TraceOutcome.read(partial(todo.assigned, proposed, None)),
                TraceOutcome.read(partial(assignment.require_trace, proposed.owner, trace)),
                TraceOutcome.read(partial(todo.assigned, proposed, trace)),
                store.get(todo.id) == todo,
            )
        return UnverifiedParity(
            "Assignment.require_trace and Todo.assigned exist, but no live pending order "
            "has two retained owners and a distinct registered assignment to compare; "
            "no fixture substituted."
        )


def scheduled_wake() -> ParityComparison:
    return UnverifiedParity(
        "CommanderHeartbeat exists, but current rotate.py was deleted. No live comparison "
        "entrypoint exposes the running heartbeat; starting another scheduler "
        "would duplicate wakes."
    )


def commander_page() -> ParityComparison:
    return UnverifiedParity(
        "Missing native owner: Toad commander page rendering the fleet through ui_model. "
        "Existing ui_model sidebar/message projections are not that page."
    )


def multiple_commanders() -> ParityComparison:
    config = CommanderConfig.read(CommanderConfig.path())
    threads = Comms(config.root).registry.all_threads()
    return UnverifiedParity(
        "Missing native owner: registry commander/project ownership and duplicate-project "
        f"commander refusal. Read the live registry ({len(threads)} threads); "
        "ordinary thread project paths do not establish commander ownership."
    )
