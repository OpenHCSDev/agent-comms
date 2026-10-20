"""Live configuration and transfer comparisons; absent workflows fail closed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ..change_report import FileLine
from ..commander.config import CommanderConfig
from ..comms import Comms
from ..thread_identity import ThreadIncarnation
from ..todos import Assignment, TodoError
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
        native.require_commander(registered)
        return cls(current, native, registered.incarnation, registered.project_path)


@dataclass(frozen=True)
class TraceOutcome:
    refusal: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refusal)

    @classmethod
    def read(cls, action: Callable[[], object]) -> TraceOutcome:
        try:
            action()
        except TodoError as error:
            return cls(str(error))
        return cls()


@dataclass(frozen=True)
class TraceCases:
    without_trace: TraceOutcome
    with_trace: TraceOutcome

    @property
    def passed(self) -> bool:
        return self.without_trace.refused and not self.with_trace.refused


@dataclass(frozen=True)
class TransferComparison(ParityComparison):
    problem: str
    current: TraceCases
    native: TraceCases
    unchanged: bool

    @property
    def passed(self) -> bool:
        return self.current == self.native and self.current.passed and self.unchanged

    def describe(self) -> str:
        return f"current=Assignment.require_trace; native=Todo.assigned; {self!r}"

    @classmethod
    def read(cls) -> ParityComparison:
        config = CommanderConfig.read(CommanderConfig.path())
        comms = Comms(config.root)
        store = comms.goals.todos
        commander = comms.registry.require(config.commander)
        registered = frozenset(
            thread.incarnation for thread in comms.registry.all_threads().values()
        )
        orders = store.held_for_commander(commander.incarnation)
        declaration = Assignment.require_trace.__code__
        trace = FileLine.from_wire(f"{declaration.co_filename}:{declaration.co_firstlineno}")
        for todo in orders:
            if not todo.pending:
                continue
            assignment = todo.held()
            for other in orders:
                proposed = other.held()
                if proposed.owner_identity not in registered:
                    continue
                if proposed.generation == assignment.generation:
                    continue
                current_without = TraceOutcome.read(
                    partial(assignment.require_trace, proposed.owner, None)
                )
                if not current_without.refused:
                    continue
                current = TraceCases(
                    current_without,
                    TraceOutcome.read(partial(assignment.require_trace, proposed.owner, trace)),
                )
                native = TraceCases(
                    TraceOutcome.read(partial(todo.assigned, proposed, None)),
                    TraceOutcome.read(partial(todo.assigned, proposed, trace)),
                )
                return cls(todo.id, current, native, store.get(todo.id) == todo)
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
