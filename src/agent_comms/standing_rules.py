"""Standing rules the commander keeps: each member derives its violations on read.

Members reading Core state are declared here; members reading the commander's own
files are declared in ``agent_comms.commander.standing`` until those files move into Core.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from itertools import combinations
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.goals import GoalChangedError
from agent_comms.recorded_time import EventTime, RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.work_branches import BranchDifference
from agent_comms.service_passes import ServicePassStore

if TYPE_CHECKING:
    from agent_comms.bus import BusReader
    from agent_comms.comms import Comms
    from agent_comms.owner_requests import OwnerRequestId, RequestDirective
    from agent_comms.messages import Message
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.storage_rules import MemoryUse, StorageRule
    from agent_comms.todos import OrderClaim, OwnerRequest, PullRequest, Todo
    from agent_comms.ui_model.unit_state import UnitState


@dataclass(frozen=True)
class Sign:
    """One occurrence of a degraded signal. Its key is what makes it new: a message, a stalled
    phase at its age doubling, a load round. The same occurrence never stops the commander twice;
    its line is how it reads now."""

    signal: type[Degradation]
    key: tuple[object, ...]
    line: str = field(compare=False)

    @property
    def mark(self) -> str:
        return self.signal.mark(self.key)


class Degradation(DeclaredFamily, affix="Degradation"):
    """One signal that the fleet is getting away from its commander."""

    @classmethod
    @abstractmethod
    def found(cls, comms: Comms, commander: str) -> tuple[Sign, ...]:
        """How the signal shows now, one sign per occurrence; empty while it does not."""

    @classmethod
    def mark(cls, key: tuple[object, ...]) -> str:
        """One occurrence as text, the same however often it is read."""
        return " ".join((cls.declared_name, *map(str, key)))


class BlocksRelease:
    """A degraded signal the commander clears this turn, so a chokehold holds its units until
    the signal is gone: a quote is the answer, never the relief."""


class StandingRule(DeclaredFamily, affix="StandingRule", discovery_package="agent_comms.commander"):
    @classmethod
    @abstractmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @abstractmethod
    def broke_after(self, after: float) -> bool: ...

    def attention(self) -> bool:
        return True

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
            lines.append(f"{prefix} {rule.heartbeat_line(new or old)}")
        return tuple(lines)

    @staticmethod
    def todos(comms: Comms, commander: str) -> tuple[Todo, ...]:
        return comms.goals.todos.held_for_commander(comms.registry.require(commander).incarnation)

    @staticmethod
    def pending_todos(comms: Comms, commander: str) -> tuple[Todo, ...]:
        return tuple(todo for todo in StandingRule.todos(comms, commander) if todo.pending)

    @staticmethod
    def holders(comms: Comms, commander: str) -> dict[ThreadIncarnation, list[Todo]]:
        """Every unit holding an open order, with its orders."""
        held: dict[ThreadIncarnation, list[Todo]] = {}
        for todo in StandingRule.todos(comms, commander):
            held.setdefault(todo.held().owner_identity, []).append(todo)
        return held

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        """Ordinary rules scan independently; a family can own one shared scan."""
        return cls

    @classmethod
    def broken(cls, comms: Comms, commander: str, after: float = 0.0, *,
               observed: Sequence[UnitState]) -> tuple[Self, ...]:
        """Read each owner once against the supplied frame, preserving the requested family."""
        owners = dict.fromkeys(rule.scan_owner() for rule in cls.members_with(cls))
        return tuple(rule for owner in owners
                     for rule in owner.scan_observed(comms, commander, observed)
                     if isinstance(rule, cls) and rule.attention() and rule.broke_after(after))


class IndependentStandingRule(StandingRule, declared_name=None):
    """A rule whose source read does not consume the unit observation frame."""

    @classmethod
    @abstractmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]: ...

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]:
        return cls.violations(comms, commander)


class UndatedStandingRule(StandingRule, declared_name=None):
    """A rule that cannot say when its condition began: it is broken on every read."""

    def broke_after(self, after: float) -> bool:
        return True


@dataclass(frozen=True)
class HeldBranch:
    """One open order joined to the holder's current recorded checkout."""

    problem: str
    holder: ThreadIncarnation
    difference: BranchDifference


class HeldBranchRule(UndatedStandingRule, declared_name=None):
    """Branch rules share the heartbeat's existing progress frame and one scan."""

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return HeldBranchRule

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]:
        branches = tuple(
            HeldBranch(todo.id, unit, state.source.pulls.branch_difference(
                state.source.thread.project_path))
            for state in observed for unit in state.readable_units() for todo in state.source.todos
        )
        ordered = tuple(sorted(branches, key=lambda held: (
            held.difference.repository.to_wire(), held.difference.branch,
            held.holder.require_recorded().lookup, held.problem,
        )))
        return tuple(rule for member in cls.members_with(cls) for rule in member.derive(ordered))

    @classmethod
    @abstractmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]: ...


@dataclass(frozen=True)
class BranchBehindMain(HeldBranchRule):
    """A held branch needs a new base after too much main work passed it."""

    problem: str
    holder: ThreadIncarnation
    repository: RepositoryName
    branch: str
    behind: int = field(compare=False)
    limit: ClassVar[int] = 30

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(cls(held.problem, held.holder, held.difference.repository,
                         held.difference.branch, held.difference.behind)
                     for held in branches if held.difference.behind > cls.limit)

    def line(self) -> str:
        return (f"BRANCH BEHIND MAIN: {self.problem} {self.holder.name} "
                f"{self.repository.to_wire()}:{self.branch} is {self.behind} commits behind "
                f"origin/main (limit {self.limit}) -> update its base")


@dataclass(frozen=True)
class SharedFileBranches(HeldBranchRule):
    """Different held branches touch the same repository-relative file."""

    problem: str
    holder: ThreadIncarnation
    branch: str
    other_problem: str
    other_holder: ThreadIncarnation
    other_branch: str
    repository: RepositoryName
    path: str

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        found = []
        for first, second in combinations(branches, 2):
            left, right = first.difference, second.difference
            if (first.holder == second.holder or left.repository != right.repository
                    or left.branch == right.branch):
                continue
            found.extend(cls(first.problem, first.holder, left.branch,
                             second.problem, second.holder, right.branch, left.repository, path)
                         for path in sorted(left.files & right.files))
        return tuple(found)

    def line(self) -> str:
        return (f"SHARED FILE BRANCHES: {self.problem} {self.holder.name} ({self.branch}) "
                f"and {self.other_problem} {self.other_holder.name} ({self.other_branch}) "
                f"touch {self.repository.to_wire()}:{self.path} -> coordinate their merge")


class DatedStandingRule(StandingRule, declared_name=None):
    """A rule that says when its condition began: it is broken on the reads after that."""

    @abstractmethod
    def began(self) -> float: ...

    def broke_after(self, after: float) -> bool:
        return self.began() >= after


@dataclass(frozen=True)
class OwedAnswer(IndependentStandingRule, declared_name=None):
    """A message to the commander owed an answer past its window. Every member gates the
    commander's answering verbs until an action names it with --answers, so an answer cannot
    be held: one that must wait is sent now as "waiting on X, owner Y"."""

    message: Message

    def degradation_signs(self) -> tuple[Sign, ...]:
        return (Sign(AnswersOwedDegradation, (self.cited,), self.line()),)

    @property
    def cited(self) -> str:
        """The owed message as the heartbeat and the gate name it."""
        return f"{self.message.seq}:{self.message.message_id}"

    @classmethod
    @abstractmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        """This member's open debts, inside their window or past it, on one bus cut."""

    def due(self, now: float) -> bool:
        return True

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        now = time.time()
        return tuple(rule for rule in cls.opened(comms, commander) if rule.due(now))

    @classmethod
    def opened(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        # Orders and request directives name already-committed messages. Capture them
        # before the bus cut so every recorded reference exists in that cut.
        held = cls.holders(comms, commander)
        snapshot = comms.registry.snapshot()
        requests = comms.goals.todos.open_requests(snapshot.require(commander).incarnation)
        with comms.bus.bus.reading() as source:
            return tuple(rule for member in cls.members_with(cls)
                         for rule in member.read_open(source, snapshot, held, commander, requests))

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        """Whether an action naming these open messages answers this one."""
        return any(message.message_id == self.message.message_id for message in named)


class AnswersOwedDegradation(Degradation, BlocksRelease):
    """Messages to the commander are waiting past their window; each message is one sign."""

    @classmethod
    def found(cls, comms: Comms, commander: str) -> tuple[Sign, ...]:
        return tuple(sign for rule in OwedAnswer.violations(comms, commander)
                     for sign in rule.degradation_signs())


@dataclass(frozen=True)
class ReasonedStandingRule(StandingRule, declared_name=None):
    """A rule broken at most once, saying why in its declared line."""

    why: str = field(compare=False)
    text: ClassVar[str]

    @classmethod
    @abstractmethod
    def reason(cls, comms: Comms, commander: str) -> str | None: ...

    @classmethod
    def found(cls, why: str, comms: Comms, commander: str,
              observed: Sequence[UnitState]) -> Self:
        return cls(why)

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]:
        why = cls.reason(comms, commander)
        return (cls.found(why, comms, commander, observed),) if why else ()

    def line(self) -> str:
        return self.text.format(why=self.why)


@dataclass(frozen=True)
class HeldOrderDue(IndependentStandingRule, DatedStandingRule, declared_name=None):
    """Each held order supplies one dated condition; the rule owns its wording."""

    problem: str
    holder: ThreadIncarnation
    order: str
    since: EventTime

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(cls(todo.id, todo.held().owner_identity, todo.order.checked().id, due)
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

    def line(self) -> str:
        return (f"TIMER UP: {self.holder.name} {self.problem} at {self.since.clock()}, "
                f"order {self.order} -> read its diff and judge it")


@dataclass(frozen=True)
class DeliveredHeadReview(DatedStandingRule):
    """A recorded delivery waits on a review of its current ready PR head."""

    problem: str
    holder: ThreadIncarnation
    order: str
    repo: RepositoryName
    pr: int
    head: str
    due_at: float
    minutes: ClassVar[float] = 5

    @classmethod
    def for_head(
        cls, problem: str, holder: ThreadIncarnation, order: str, claim: OrderClaim,
        request: PullRequest, now: float,
    ) -> tuple[Self, ...]:
        due_at = claim.message.timestamp + cls.minutes * 60
        if request.draft or request.has_head_review or now < due_at:
            return ()
        return (cls(problem, holder, order, claim.pull_request.repo,
                    request.number, request.head, due_at),)

    @classmethod
    def scan_observed(
        cls, comms: Comms, commander: str, observed: Sequence[UnitState],
    ) -> tuple[Self, ...]:
        pulls = next((state.source.pulls for state in observed), None)
        now = time.time()
        return tuple(
            rule
            for todo, claim, request in comms.goals.todos.claimed_heads_for_commander(
                comms.registry.require(commander).incarnation, pulls,
            )
            for rule in cls.for_head(
                todo.id, todo.held().owner_identity, todo.order.checked().id,
                claim, request, now,
            )
        )

    def began(self) -> float:
        return self.due_at

    def line(self) -> str:
        return (f"DELIVERED HEAD NEEDS REVIEW: order {self.order} ({self.problem}), "
                f"holder {self.holder.name}, {self.repo.to_wire()}#{self.pr} head {self.head} "
                "-> commander reviews this head")


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
    def found(cls, why: str, comms: Comms, commander: str,
              observed: Sequence[UnitState]) -> HuntDue:
        since = cls.hunted(comms, commander) + cls.seconds
        readable = {unit for state in observed for unit in state.readable_units()}
        todos = tuple(todo for todo in cls.todos(comms, commander)
                      if todo.held().owner_identity in readable)
        return next((LocatedHuntDue(why, since, path)
                     for path in cls.next_hunt(comms, commander, todos)), cls(why, since))

    @classmethod
    def next_hunt(cls, comms: Comms, commander: str,
                  todos: Iterable[Todo]) -> tuple[str, ...]:
        """Least recently hunted hot file in the held orders' declared check repositories."""
        actor = comms.registry.require(commander).incarnation
        hunted: dict[str, float] = {}
        for hunt in comms.goals.todos.hunts_for_commander(actor):
            hunted[hunt.where.file] = max(hunted.get(hunt.where.file, 0),
                                          hunt.at.recorded_at("A hunt"))
        repos = {Repo(checkout=Path(todo.order.checked().check.directory), github=todo.repo)
                 for todo in todos}
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
class DirectiveUnacted(OwedAnswer, DatedStandingRule):
    """An owner directive recorded on an open request, past its deadline with no order since."""

    request: OwnerRequestId
    directive: RequestDirective

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(
            cls(original, request.id, directive)
            for request in requests
            for directive, original in request.directive_messages(source, snapshot.names_for(commander))
        )

    def due(self, now: float) -> bool:
        return self.directive.deadline < now

    def began(self) -> float:
        return self.directive.deadline

    def line(self) -> str:
        return (
            f"DIRECTIVE UNACTED: owner directive {self.cited}"
            f" on request {self.request.value},"
            f" due {self.directive.due_time} -> order its next step or mark the request done"
        )

    def heartbeat_group(self) -> object:
        return type(self), self.request

    def heartbeat_line(self, current: Sequence[Self]) -> str:
        oldest = min(current, key=lambda rule: rule.directive.seq).directive
        newest = max(current, key=lambda rule: rule.directive.seq).directive
        return (
            f"DIRECTIVE UNACTED: request {self.request.value}: {len(current)} owner directives; "
            f"oldest {oldest.seq} due {oldest.due_time}; "
            f"newest {newest.seq} due {newest.due_time} "
            "-> order the next step or mark the request done"
        )


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
class ServicePassFailing(IndependentStandingRule, DatedStandingRule):
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
class BoardDisagrees(IndependentStandingRule, UndatedStandingRule):
    """An open order reads as no commit despite its linked pull request having merged."""

    problem: str
    order: str
    merge: str

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(
            cls(todo.id, todo.order.checked().id, merged.sha)
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
class MemoryLow(IndependentStandingRule, UndatedStandingRule):
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
class DiskLow(IndependentStandingRule, UndatedStandingRule):
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
