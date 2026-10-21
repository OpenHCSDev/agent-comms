"""Standing rules the commander keeps: each member derives its violations on read.

Members reading Core state are declared here; members reading the commander's own
files are declared in ``agent_comms.commander.standing`` until those files move into Core.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.goals import GoalChangedError
from agent_comms.recorded_time import EventTime, RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.service_passes import ServicePassStore

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goals import RequestDirective
    from agent_comms.message_reference import MessageReference
    from agent_comms.commander_pings import Ping
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.ui_model.unit_state import UnitState
    from agent_comms.storage_rules import MemoryUse, StorageRule
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.todos import Todo


class StandingRule(DeclaredFamily, affix="StandingRule", discovery_package="agent_comms.commander"):
    @classmethod
    @abstractmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @abstractmethod
    def broke_after(self, after: float) -> bool: ...

    def unit_states(self) -> tuple[UnitState, ...]:
        return ()

    def commander_pings(self, todos: Mapping[str, Todo],
                        states: Mapping[ThreadIncarnation, UnitState]) -> tuple[Ping, ...]:
        """Only rules that declare a commander decision contribute a ping."""
        return ()

    def rejected_line(self) -> str:
        return f"Not applied (goal changed): {self.line()}"

    def enforce(self, comms: Comms, commander: str) -> None:
        """What Core does itself when this rule breaks; a rule that only reports does nothing."""

    @staticmethod
    def enforce_all(comms: Comms, commander: str,
                    rules: Iterable[StandingRule]) -> frozenset[StandingRule]:
        rejected: set[StandingRule] = set()
        for rule in rules:
            try:
                rule.enforce(comms, commander)
            except GoalChangedError as changed:
                logging.info("Skipped stale enforcement: %s", changed)
                rejected.add(rule)
        return frozenset(rejected)

    @staticmethod
    def todos(comms: Comms, commander: str) -> tuple[Todo, ...]:
        return comms.goals.todos.held_for_commander(comms.registry.require(commander).incarnation)

    @staticmethod
    def pending_todos(comms: Comms, commander: str) -> tuple[Todo, ...]:
        return tuple(todo for todo in StandingRule.todos(comms, commander) if todo.pending)

    @staticmethod
    def holders(comms: Comms, commander: str) -> dict[ThreadIncarnation, list[Todo]]:
        """Every unit holding a pending order, with its orders."""
        held: dict[ThreadIncarnation, list[Todo]] = {}
        for todo in StandingRule.pending_todos(comms, commander):
            held.setdefault(todo.held().owner_identity, []).append(todo)
        return held

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        """Ordinary rules scan independently; a family can own one shared scan."""
        return cls

    @classmethod
    def broken(cls, comms: Comms, commander: str, after: float = 0.0) -> tuple[StandingRule, ...]:
        """Read each inherited scan owner once, preserving the requested member scope."""
        owners = dict.fromkeys(rule.scan_owner() for rule in cls.members_with(cls))
        found = (v for owner in owners for v in owner.violations(comms, commander))
        return tuple(rule for rule in found if isinstance(rule, cls) and rule.broke_after(after))


class OrderRefusingStandingRule(StandingRule, declared_name=None):
    """A broken rule owns the reason an unrelated commander order cannot proceed."""

    @abstractmethod
    def refusal(self, comms: Comms, answers: Sequence[MessageReference]) -> tuple[str, ...]: ...


class UndatedStandingRule(StandingRule, declared_name=None):
    """A rule that cannot say when its condition began: it is broken on every read."""

    def broke_after(self, after: float) -> bool:
        return True


class DatedStandingRule(StandingRule, declared_name=None):
    """A rule that says when its condition began: it is broken on the reads after that."""

    @abstractmethod
    def began(self) -> float: ...

    def broke_after(self, after: float) -> bool:
        return self.began() >= after


@dataclass(frozen=True)
class ReasonedStandingRule(StandingRule, declared_name=None):
    """A rule broken at most once, saying why in its declared line."""

    why: str = field(compare=False)
    text: ClassVar[str]

    @classmethod
    @abstractmethod
    def reason(cls, comms: Comms, commander: str) -> str | None: ...

    @classmethod
    def found(cls, why: str, comms: Comms, commander: str) -> Self:
        return cls(why)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        why = cls.reason(comms, commander)
        return (cls.found(why, comms, commander),) if why else ()

    def line(self) -> str:
        return self.text.format(why=self.why)


@dataclass(frozen=True)
class HeldOrderDue(DatedStandingRule, declared_name=None):
    """Each held order supplies one dated condition; the rule owns its wording."""

    problem: str
    holder: str
    order: str
    since: EventTime

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(cls(todo.id, todo.held().owner, todo.order.checked().id, due)
                     for todo, due in cls.conditions(comms, commander))

    @classmethod
    @abstractmethod
    def conditions(cls, comms: Comms, commander: str) -> Iterable[tuple[Todo, EventTime]]: ...

    def began(self) -> float:
        return self.since.recorded_at("An order alarm")


class ReviewTimerUp(HeldOrderDue):
    """Each round of a held order's review timer is its own condition, so the heartbeat wakes
    the commander once per round; the unit sees no timer and nothing is struck."""

    @classmethod
    def conditions(cls, comms: Comms, commander: str) -> Iterable[tuple[Todo, EventTime]]:
        actor = comms.registry.require(commander).incarnation
        return comms.goals.todos.timers_up_for_commander(
            actor, comms.registry, time.time())

    def commander_pings(self, todos, states):
        from .commander_pings import OrderRevision, ReviewTimerPing
        from .ui_model.unit_state import Moving

        todo = todos[self.problem]
        unit = todo.held().owner_identity
        state = type(states[unit]) if unit in states else Moving
        return (ReviewTimerPing(unit, state, (OrderRevision.of(todo),),
                                self.since.recorded_at("A review timer")),)

    def line(self) -> str:
        return (f"TIMER UP: {self.holder} {self.problem} at {self.since.clock()}, "
                f"order {self.order} -> read its diff and judge it")


@dataclass(frozen=True)
class HuntDue(ReasonedStandingRule, DatedStandingRule):
    since: float
    seconds: ClassVar[int] = 900

    @classmethod
    def hunted(cls, comms: Comms, commander: str) -> float:
        hunts = comms.goals.todos.hunts_for_commander(
            comms.registry.require(commander).incarnation)
        return max((hunt.at.recorded_at("A hunt") for hunt in hunts), default=0)

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        age = time.time() - cls.hunted(comms, commander)
        return f"last hunt {int(age // 60)} min ago" if age > cls.seconds else None

    @classmethod
    def found(cls, why: str, comms: Comms, commander: str) -> HuntDue:
        since = cls.hunted(comms, commander) + cls.seconds
        return next((LocatedHuntDue(why, since, path)
                     for path in cls.next_hunt(comms, commander)), cls(why, since))

    @classmethod
    def next_hunt(cls, comms: Comms, commander: str) -> tuple[str, ...]:
        """Least recently hunted hot file in the held orders' declared check repositories."""
        actor = comms.registry.require(commander).incarnation
        hunted: dict[str, float] = {}
        for hunt in comms.goals.todos.hunts_for_commander(actor):
            hunted[hunt.where.file] = max(hunted.get(hunt.where.file, 0),
                                          hunt.at.recorded_at("A hunt"))
        repos = {Repo(checkout=Path(todo.order.checked().check.directory), github=todo.repo)
                 for todo in cls.todos(comms, commander)}
        candidates: list[str] = []
        for repo in repos:
            log = repo.git(repo.checkout, 'log', 'origin/main', '--since=7.days',
                           '--name-only', '--format=', '--', repo.source)
            churn = Counter(str((repo.checkout / name).resolve())
                            for name in log.splitlines()
                            if name.endswith('.py') and (repo.checkout / name).is_file())
            candidates.extend(path for path, _ in churn.most_common(15))
        return tuple(sorted(set(candidates), key=lambda path: (hunted.get(path, 0), path)))[:1]

    def line(self) -> str:
        return ("HUNT DUE: read a held order's production source (no hot file found) "
                "and order the fix this wake")

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class LocatedHuntDue(HuntDue, declared_name=None):
    where: str

    def line(self) -> str:
        return f"HUNT DUE: read {self.where} and order the fix this wake"


@dataclass(frozen=True)
class DirectiveUnacted(OrderRefusingStandingRule, DatedStandingRule):
    """A claude-ai directive recorded on an open request, past its deadline with no order since."""

    request: str
    directive: RequestDirective

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        thread, now = comms.registry.require(commander), time.time()
        return tuple(
            cls(goal.id, directive)
            for goal in thread.goal.active()
            for directive in goal.order.overdue_directives(
                goal, comms.goals, thread.incarnation, now
            )
        )

    def began(self) -> float:
        return self.directive.deadline

    def line(self) -> str:
        return (
            f"DIRECTIVE UNACTED: claude-ai {self.directive.seq} on request {self.request},"
            f" due {self.directive.due_time} -> order its next step or mark the request done"
        )

    def refusal(self, comms: Comms, answers: Sequence[MessageReference]) -> tuple[str, ...]:
        message = self.directive.original(comms.goals)
        if message.reference in answers:
            return ()
        return (f"{message.message_id}: overdue owner directive on request {self.request}; "
                "reply to the original message or use --answers MESSAGE_ID.",)


@dataclass(frozen=True)
class BridgeDown(ReasonedStandingRule, UndatedStandingRule):
    text = (
        "COMMS BRIDGE DOWN: the commander receives nothing -> re-arm the bridge Monitor this wake"
    )

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        receiving = comms.registry.snapshot().status(commander).receiving
        return None if receiving else f"{commander} is not receiving"


@dataclass(frozen=True)
class ServicePassFailing(DatedStandingRule):
    """A Core service watch whose newest pass failed; the connector's log reaches nobody."""

    watch: str
    since: float
    error: str = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        failing = ServicePassStore.at(comms.root).read().failing
        return tuple(cls(watch, failed.since, failed.error)
                     for watch, failed in sorted(failing.items()))

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return (f"SERVICE PASS FAILING: {self.watch} since "
                f"{RecordedEventTime(self.since).clock()}: {self.error}"
                " -> fix the owner of the failing read this wake")

@dataclass(frozen=True)
class BoardDisagrees(UndatedStandingRule):
    """An open order reads as no commit despite its linked pull request having merged."""

    problem: str
    order: str
    merge: str

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(
            cls(todo.id, todo.order.id, merged.sha)
            for todo, merged in comms.goals.todos.board_disagreements(
                comms.registry.require(commander).incarnation, comms.registry
            )
        )

    def line(self) -> str:
        return (
            f"BOARD DISAGREES: {self.problem} order {self.order} reads no commit, "
            f"but its linked PR merged as {self.merge[:9]} -> repair the progress read this wake"
        )


@dataclass(frozen=True)
class MemoryLow(UndatedStandingRule):
    """Under the memory threshold a cleanup owner is due, told what holds the memory."""

    rule: StorageRule
    memory: MemoryUse = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        rule = comms.registry.storage_rule
        memory = rule.memory()
        return (cls(rule, memory),) if rule.memory_due(memory) else ()

    def line(self) -> str:
        dirs = ", ".join(map(str, self.memory.idle_dirs)) or "none"
        processes = ", ".join(map(str, self.memory.processes)) or "none"
        return (
            f"MEMORY LOW: {self.memory.available_gib:.1f} GiB available (cleanup due under"
            f" {self.rule.memory_gib:g} GiB); pytest temp dirs idle over"
            f" {self.rule.idle_minutes:g} min: {dirs}; test processes: {processes}"
            " -> put a cleanup owner on it now"
        )


@dataclass(frozen=True)
class DiskLow(UndatedStandingRule):
    """Under the cleanup threshold a cleanup owner is due; under the floor nothing allocates."""

    rule: StorageRule
    free_gib: float = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[DiskLow, ...]:
        rule = comms.registry.storage_rule
        free = rule.free_gib()
        member = DiskFloorLow if rule.below_floor(free) else cls
        return (member(rule, free),) if rule.cleanup_due(free) else ()

    def floor_warning(self) -> str:
        return ""

    def line(self) -> str:
        return (
            f"DISK LOW: {self.rule.volume} has {self.free_gib:.1f} GiB free (cleanup due under"
            f" {self.rule.cleanup_gib:g} GiB{self.floor_warning()})"
            " -> put a cleanup owner on it now"
        )


@dataclass(frozen=True)
class DiskFloorLow(DiskLow, declared_name=None):
    """The allocation-floor warning, selected by DiskLow rather than a second rule scan."""

    def floor_warning(self) -> str:
        return f"; under the {self.rule.floor_gib:g} GiB floor nothing allocates"
