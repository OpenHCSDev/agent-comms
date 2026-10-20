"""Standing rules the commander keeps: each member derives its violations on read.

Members reading Core state are declared here; members reading the commander's own
files are declared in ``agent_comms.commander.standing`` until those files move into Core.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .declared_family import DeclaredFamily
from .plan_phases import PlanPhase
from .rule_reviews import RuleReviews

if TYPE_CHECKING:
    from .comms import Comms
    from .goals import RequestDirective
    from .storage_rules import MemoryUse, StorageRule
    from .todos import Todo


class StandingRule(DeclaredFamily, affix="StandingRule", discovery_package="agent_comms.commander"):
    @classmethod
    @abstractmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @abstractmethod
    def broke_after(self, after: float) -> bool: ...

    def enforce(self, comms: Comms, commander: str) -> None:
        """What Core does itself when this rule breaks; a rule that only reports does nothing."""

    def heartbeat_group(self) -> object:
        """Ordinary violations each own one heartbeat line."""
        return self

    def heartbeat_line(self, current: Sequence[Self]) -> str:
        return self.line()

    @staticmethod
    def _heartbeat_groups(rules: Iterable[StandingRule]) -> dict[object, list[StandingRule]]:
        groups: dict[object, list[StandingRule]] = defaultdict(list)
        for rule in rules:
            groups[rule.heartbeat_group()].append(rule)
        return groups

    @classmethod
    def heartbeat_changes(cls, previous: Iterable[StandingRule],
                          current: Iterable[StandingRule]) -> tuple[str, ...]:
        """Render changed groups through their owning rule, without replaying unchanged ones."""
        before, after = cls._heartbeat_groups(previous), cls._heartbeat_groups(current)
        lines = []
        for group in dict.fromkeys((*after, *before)):
            old, new = before.get(group, []), after.get(group, [])
            if frozenset(old) == frozenset(new):
                continue
            rule = (new or old)[0]
            prefix = "RULE" if new else "NO LONGER CURRENT:"
            lines.append(f"{prefix} {rule.heartbeat_line(new)}")
        return tuple(lines)

    @staticmethod
    def todos(comms: Comms, commander: str) -> tuple[Todo, ...]:
        return comms.goals.todos.held_for_commander(comms.registry.require(commander).incarnation)

    @classmethod
    def broken(cls, comms: Comms, commander: str, after: float = 0.0) -> tuple[StandingRule, ...]:
        """Every member's violations for one commander that began after ``after``."""
        found = (v for rule in cls.members_with(cls) for v in rule.violations(comms, commander))
        return tuple(rule for rule in found if rule.broke_after(after))


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
class HuntDue(ReasonedStandingRule, DatedStandingRule):
    since: float
    seconds: ClassVar[int] = 900
    text = "HUNT DUE: read a hot path and order the fix this wake"

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
    def found(cls, why: str, comms: Comms, commander: str) -> Self:
        return cls(why, cls.hunted(comms, commander) + cls.seconds)

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class DirectiveUnacted(DatedStandingRule):
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

    def heartbeat_group(self) -> object:
        return type(self), self.request

    def heartbeat_line(self, current: Sequence[Self]) -> str:
        if not current:
            return (f"DIRECTIVE UNACTED: request {self.request}: 0 claude-ai directives; "
                    "oldest none; newest none")
        oldest = min(current, key=lambda rule: rule.directive.seq).directive
        newest = max(current, key=lambda rule: rule.directive.seq).directive
        return (
            f"DIRECTIVE UNACTED: request {self.request}: {len(current)} claude-ai directives; "
            f"oldest {oldest.seq} due {oldest.due_time}; "
            f"newest {newest.seq} due {newest.due_time} "
            "-> order the next step or mark the request done"
        )


@dataclass(frozen=True)
class RulesDue(DatedStandingRule):
    """A reviewer acts only while its last review is fresh and every drawn rule is answered."""

    max_age: ClassVar[int] = 20 * 60
    why: str = field(compare=False)
    since: float

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return RuleReviews(comms.root).review(commander).due(cls, commander)

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return (
            f"RULES DUE ({self.why}) -> agent-comms-commander rules review"
            " and answer the drawn rules before any order"
        )


@dataclass(frozen=True)
class BridgeDown(ReasonedStandingRule, UndatedStandingRule):
    text = (
        "COMMS BRIDGE DOWN: the commander receives nothing -> re-arm the bridge Monitor this wake"
    )

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        receiving = comms.registry.snapshot().statuses[commander].receiving
        return None if receiving else f"{commander} is not receiving"


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
                comms.registry.require(commander).incarnation, comms.registry.storage_rule
            )
        )

    def line(self) -> str:
        return (
            f"BOARD DISAGREES: {self.problem} order {self.order} reads no commit, "
            f"but its linked PR merged as {self.merge[:9]} -> repair the progress read this wake"
        )


@dataclass(frozen=True)
class PlanIdle(UndatedStandingRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        todos = comms.goals.todos.history_for_commander(
            comms.registry.require(commander).incarnation
        )
        return tuple(
            cls(phase)
            for phase in PlanPhase.members_with(PlanPhase)
            if phase.ready(todos) and not phase.ordered(todos)
        )

    def line(self) -> str:
        return (
            f"PLAN IDLE: phase {self.phase.declared_name} ready, no open order"
            " -> dispatch it this wake"
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
