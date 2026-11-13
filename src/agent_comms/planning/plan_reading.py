"""One look at the plans: the order history, the plan repository's main and their lifetime."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from itertools import chain
from subprocess import TimeoutExpired
from threading import Lock
from typing import ClassVar

from agent_comms.planning.orders import Todo
from agent_comms.planning.phase_evidence import (
    Census,
    EvidenceFound,
    EvidenceRead,
    EvidenceUnread,
    MainTree,
    PhaseEvidence,
    UnreadableEvidenceError,
)
from agent_comms.projects.checkouts import Checkout, CheckoutHistory
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo


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

    def current(self, todos: tuple[Todo, ...], main: MainTree) -> PlanReading:
        """Within one pass the same commit shares this reading's answers and parsed facts;
        a moved main carries only what the new commit still supports."""
        if main == self.main:
            return replace(self, todos=todos)
        return self.following(todos, main)

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
        return reading

    def read(self, repo: Repo, todos: tuple[Todo, ...],
             evidence: tuple[PhaseEvidence, ...]) -> PlanReading:
        return self.observe_reading(self.prepare_reading(
            repo, todos, evidence, history=Checkout(repo.checkout).read()), evidence)

    def finish_pass(self) -> None:
        """An ordinary command releases its reading when its caller does."""


class RetainedPlanReadings(PlanReadings):
    """Keep completed private snapshots; every reading prepared in one pass shares one answer
    per repository commit, so served teammates never re-parse or re-evaluate it."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.plans: tuple[PlanReading, ...] = ()
        self.prepared: tuple[PlanReading, ...] = ()
        self.lock = Lock()

    def begin_pass(self) -> None:
        """A new pass asks every source again; an unread answer is never carried into it."""
        with self.lock:
            self.prepared = ()

    def observed(
        self, repo: Repo, todos: tuple[Todo, ...], history: CheckoutHistory,
    ) -> PlanReading:
        current = super().observed(repo, todos, history)
        with self.lock:
            prepared, plans = self.prepared, self.plans
        reading = next(chain(
            (plan.current(todos, current.main) for plan in prepared if plan.main.repo == repo),
            (plan.following(todos, current.main) for plan in plans if plan.main.repo == repo),
        ), current)
        with self.lock:
            self.prepared = (*(plan for plan in self.prepared if plan.main.repo != repo), reading)
        return reading

    def finish_pass(self) -> None:
        """Retain each repository's one shared reading of this pass, once, however many
        teammates observed it; the next pass prepares from what was retained."""
        with self.lock:
            prepared, self.prepared = self.prepared, ()
        retained = tuple(
            reading.following(reading.todos, reading.main._refreshed_history())
            for reading in prepared
        )
        repos = {reading.main.repo for reading in retained}
        with self.lock:
            self.plans = (*(plan for plan in self.plans if plan.main.repo not in repos),
                          *retained)
