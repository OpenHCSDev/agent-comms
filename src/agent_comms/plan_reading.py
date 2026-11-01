"""One look at the plans: the order history, the plan repository's main and their lifetime."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from threading import Lock
from subprocess import TimeoutExpired
from typing import ClassVar
from agent_comms.phase_evidence import (
    Census, EvidenceFound, EvidenceRead, EvidenceUnread, MainTree, PhaseEvidence,
    UnreadableEvidenceError,
)
from agent_comms.repo import Checkout, CheckoutHistory, GitCommandError, Repo

from agent_comms.todos import Todo



@dataclass(frozen=True)
class PlanReading:
    """One look at the plans: the order history and the plan repository's main.

    Each evidence is read once per reading, however many phases wait on it."""

    todos: tuple[Todo, ...]
    main: MainTree
    reads: dict[PhaseEvidence, EvidenceRead] = field(
        default_factory=dict, compare=False, repr=False
    )

    @contextmanager
    def source(self, evidence: PhaseEvidence) -> Iterator[None]:
        """One failure boundary for acquiring and observing the evidence's source."""
        try:
            yield
        except (GitCommandError, OSError, TimeoutExpired, UnreadableEvidenceError) as error:
            self.reads[evidence] = EvidenceUnread(str(error))

    def prepare(self, evidence: PhaseEvidence) -> None:
        if evidence not in self.reads:
            with self.source(evidence):
                evidence.prepare(self)

    def read(self, evidence: PhaseEvidence) -> EvidenceRead:
        if evidence not in self.reads:
            with self.source(evidence):
                self.reads[evidence] = EvidenceFound(evidence.holds(self))
        return self.reads[evidence]

    def following(self, todos: tuple[Todo, ...], main: MainTree) -> PlanReading:
        main = self.main.following(main)
        return type(self)(todos, main, {
            evidence: answer for evidence, answer in self.reads.items()
            if answer.retained(evidence, self.main, main)
        })

    def holds(self, evidence: PhaseEvidence) -> bool:
        return self.read(evidence).held()

    def describe(self, evidence: PhaseEvidence) -> str:
        return self.read(evidence).describe(evidence)


class PlanReadings:
    """A plan read's lifetime; ordinary commands take one explicit snapshot. The member that
    reads plans declares the census its trees are counted with."""

    census: ClassVar[Census]

    def observed(self, repo: Repo, todos: tuple[Todo, ...], history: CheckoutHistory) -> PlanReading:
        return PlanReading(todos, MainTree.from_history(repo, self.census, history))

    def prepare_reading(self, repo: Repo, todos: tuple[Todo, ...],
                evidence: tuple[PhaseEvidence, ...], *, history: CheckoutHistory) -> PlanReading:
        """Capture the main cut and acquire inputs without evaluating the plan."""
        reading = self.observed(repo, todos, history)
        for declared in evidence:
            reading.prepare(declared)
        return reading

    def observe_reading(self, reading: PlanReading, evidence: tuple[PhaseEvidence, ...]) -> PlanReading:
        for declared in evidence:
            declared.recorded(reading)
        self.retain(reading)
        return reading

    def read(self, repo: Repo, todos: tuple[Todo, ...],
             evidence: tuple[PhaseEvidence, ...]) -> PlanReading:
        return self.observe_reading(self.prepare_reading(
            repo, todos, evidence, history=Checkout(repo.checkout).read()), evidence)

    def retain(self, reading: PlanReading) -> None:
        """An ordinary command releases its reading when its caller does."""
        pass


class RetainedPlanReadings(PlanReadings):
    """Keep completed private snapshots; consumers may extend their own readings."""

    def __init__(self) -> None:
        super().__init__()
        self.plans: tuple[PlanReading, ...] = ()
        self.lock = Lock()

    def observed(
        self, repo: Repo, todos: tuple[Todo, ...], history: CheckoutHistory,
    ) -> PlanReading:
        current = super().observed(repo, todos, history)
        with self.lock:
            plans = self.plans
        for previous in plans:
            if previous.main.repo == repo:
                return previous.following(todos, current.main)
        return current

    def retain(self, reading: PlanReading) -> None:
        # Keep the pinned commit, compact blob facts and stable answers, not syntax or the graph.
        retained = reading.following(reading.todos, replace(
            reading.main, history=Checkout(reading.main.repo.checkout).read()))
        with self.lock:
            self.plans = (*(plan for plan in self.plans
                            if plan.main.repo != retained.main.repo), retained)
