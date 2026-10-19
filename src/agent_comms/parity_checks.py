"""Declared commander comparisons and their actual observations.

A missing comparison is unverified, never a passing feature. The phase owns this
check family; row identity, numbering and reporting derive from its declarations.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .cli_result import CliResult
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, projected
from .plan_phases import PlanPhase

if TYPE_CHECKING:
    from .todos import OrderWords, Todo
    from .work_dispatch import PhaseTask


class ProbeObservation(DeclaredFamily, affix="Observation"):
    accepted: ClassVar[bool]


@dataclass(frozen=True)
class AcceptedObservation(ProbeObservation):
    accepted = True


@dataclass(frozen=True)
class RefusedObservation(ProbeObservation):
    reason: str
    accepted = False


@dataclass(frozen=True)
class TextSample:
    family: type[OrderWords]
    text: str
    accepted: bool


class ParityObservation(DeclaredFamily, affix="Observation"):
    @property
    @abstractmethod
    def passed(self) -> bool: ...


@dataclass(frozen=True)
class TextProbeResult(ParityObservation):
    family: str
    text: str
    expected_acceptance: bool
    native: ProbeObservation
    current: ProbeObservation

    @projected(view="wire")
    def passed(self) -> bool:
        return self.native.accepted == self.current.accepted == self.expected_acceptance


@dataclass(frozen=True)
class CommanderComparison:
    """The current script's real boundary, supplied only in an isolated process."""

    refuse: Callable[[str, type[OrderWords]], None]

    def text(self, sample: TextSample) -> TextProbeResult:
        try:
            FieldCodec.decode(sample.family, sample.text)
        except ValueError as error:
            native = RefusedObservation(str(error))
        else:
            native = AcceptedObservation()
        try:
            self.refuse(sample.text, sample.family)
        except SystemExit as error:
            current = RefusedObservation(str(error))
        else:
            current = AcceptedObservation()
        return TextProbeResult(sample.family.__name__, sample.text, sample.accepted,
                               native, current)


class ComparisonCoverage(DeclaredFamily, affix="Coverage"):
    complete: ClassVar[bool]


@dataclass(frozen=True)
class PartialCoverage(ComparisonCoverage):
    missing: tuple[str, ...]
    complete = False

    def __post_init__(self) -> None:
        if not self.missing:
            raise ValueError("A partial comparison names what it did not exercise")


@dataclass(frozen=True)
class CompleteCoverage(ComparisonCoverage):
    complete = True


class CommanderParityCheck(
    DeclaredFamily, affix="ParityCheck", discovery_package="agent_comms.commander_parity"
):
    number: ClassVar[int]
    phases: ClassVar[tuple[type[PlanPhase], ...]]
    capability: ClassVar[str]
    coverage: ClassVar[ComparisonCoverage]

    @classmethod
    @abstractmethod
    def compare(cls, context: CommanderComparison) -> ParityRowResult: ...

    @classmethod
    def task(cls) -> PhaseTask:
        from .work_dispatch import UnspecifiedPhaseTask

        return UnspecifiedPhaseTask()

    @classmethod
    def done(cls, todos: Iterable[Todo]) -> bool:
        # Probe agreement alone never accepts a row. Its checked task must also
        # have passed and received the existing order's application acceptance.
        return cls.coverage.complete and cls.task().done(todos)

    @classmethod
    def all_done(cls, todos: Iterable[Todo]) -> bool:
        checks = cls.members_with(cls)
        history = tuple(todos)
        return bool(checks) and all(check.done(history) for check in checks)


class UnimplementedParityCheck(CommanderParityCheck, declared_name=None):
    coverage = PartialCoverage(("The native/current workflow comparison is not implemented.",))

    @classmethod
    def compare(cls, context: CommanderComparison) -> ParityRowResult:
        return ParityRowResult(cls, ())


class TextParityCheck(CommanderParityCheck, declared_name=None):
    samples: ClassVar[tuple[TextSample, ...]]
    coverage = PartialCoverage((
        "Only native field decoding and current script refusal samples ran; "
        "the row's complete verdict/order/correction workflow has not run.",
    ))

    @classmethod
    def compare(cls, context: CommanderComparison) -> ParityRowResult:
        return ParityRowResult(cls, tuple(context.text(sample) for sample in cls.samples))


@dataclass(frozen=True)
class ParityRowResult:
    check: type[CommanderParityCheck]
    observations: tuple[ParityObservation, ...]

    @projected(view="wire")
    def number(self) -> int:
        return self.check.number

    @projected(view="wire")
    def coverage(self) -> ComparisonCoverage:
        return self.check.coverage

    @projected(view="wire")
    def phases(self) -> tuple[type[PlanPhase], ...]:
        return self.check.phases

    @projected(view="wire")
    def passed(self) -> bool:
        return bool(self.observations) and all(item.passed for item in self.observations)


@dataclass(frozen=True)
class CommanderParityReport(CliResult):
    comparisons: tuple[ParityRowResult, ...]
    unverified_rows: tuple[int, ...]

    @projected(view="wire")
    def passed(self) -> bool:
        return bool(self.comparisons) and not self.unverified_rows and all(
            item.passed for item in self.comparisons)

    @projected(view="wire")
    def failing_rows(self) -> tuple[int, ...]:
        return tuple(item.check.number for item in self.comparisons
                     if item.observations and not item.passed)

    def exit_code(self) -> int:
        return 0 if self.passed else 1
