"""Standing rules the commander keeps: each member derives its violations on read.

Members reading Core state are declared here; members reading the commander's own
files are declared in ``agent_comms.commander.standing`` until those files move into Core.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from itertools import combinations
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.errors import RejectedEnforcementError
from agent_comms.text_digest import TextDigest
from agent_comms.recorded_time import EventTime, RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.head_review import PullRequestHead
from agent_comms.held_orders import HeldOrders
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.work_branches import BranchDifference
from agent_comms.service_passes import ServicePassStore

if TYPE_CHECKING:
    from agent_comms.bus import BusReader
    from agent_comms.commander.order import CommanderVerb
    from agent_comms.comms import Comms
    from agent_comms.heartbeat_frame import HeartbeatFrame
    from agent_comms.owner_requests import OwnerRequestId, RequestDirective
    from agent_comms.messages import Message
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.storage_rules import MemoryUse, StorageRule
    from agent_comms.todos import (
        OrderClaim, OwnerRequest, PresentCheckout, ProgressRead, PullRequest, Todo,
    )
    from agent_comms.goal_management import Goals
    from agent_comms.ui_model.unit_state import UnitObservation, UnitState


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
    def found(cls, comms: Comms, commander: str, frame: HeartbeatFrame) -> tuple[Sign, ...]:
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
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @abstractmethod
    def broke_after(self, after: float) -> bool: ...

    def attention(self) -> bool:
        return True

    def rejected_line(self, error: RejectedEnforcementError) -> str:
        return f"Not applied ({error.reason}): {self.line()}"

    def enforce(self, comms: Comms, commander: str) -> None:
        """What Core does itself when this rule breaks; a rule that only reports does nothing."""

    @staticmethod
    def enforce_all(
        comms: Comms,
        commander: str,
        rules: Iterable[StandingRule],
    ) -> Mapping[StandingRule, RejectedEnforcementError]:
        rejected: dict[StandingRule, RejectedEnforcementError] = {}
        for rule in rules:
            try:
                rule.enforce(comms, commander)
            except RejectedEnforcementError as error:
                rejected[rule] = error
        return rejected

    def comparison_values(self) -> tuple[object, ...]:
        """The declaration's equality fields, excluding presentation and live readers."""
        return tuple(getattr(self, item.name) for item in fields(self) if item.compare)

    def observation_key(self) -> TextDigest:
        """Stable content identity of this occurrence, never a digest of age or wording.

        Python's hash changes across processes. The codec gives the comparison
        values their declared encoding; the class's own name distinguishes siblings
        even when they deliberately have no public StandingRule registration.
        """
        return TextDigest(FieldCodec.comparison_digest((type(self), self.comparison_values())))

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        """Ordinary rules scan independently; a family can own one shared scan."""
        return cls

    @classmethod
    def broken(cls, comms: Comms, commander: str, after: float = 0.0, *,
               observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        """Read each owner once against the supplied frame and its one progress reader,
        preserving the requested family."""
        owners = dict.fromkeys(rule.scan_owner() for rule in cls.members_with(cls))
        return tuple(rule for owner in owners
                     for rule in owner.scan_observed(comms, commander, observed, frame)
                     if isinstance(rule, cls) and rule.attention() and rule.broke_after(after))


class OrderRefusingStandingRule(StandingRule, declared_name=None):
    """An overdue review prevents new orders, through the same backend admission path."""

    @classmethod
    @abstractmethod
    def scan_reviews(cls, goals: Goals, commander: str, pulls: ProgressRead) -> tuple[Self, ...]: ...

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.scan_reviews(comms.goals, commander, frame.pulls)

    @classmethod
    def refusals(cls, goals: Goals, commander: str) -> tuple[str, ...]:
        from agent_comms.todos import ProgressRead

        pulls = ProgressRead(goals.todos.held_for_commander(goals.registry.require(commander).incarnation))
        owners = dict.fromkeys(member.scan_owner() for member in cls.members_with(cls))
        return tuple(rule.line() for owner in owners
                     for rule in owner.scan_reviews(goals, commander, pulls))


class IndependentStandingRule(StandingRule, declared_name=None):
    """A rule whose source read does not consume the unit observation frame."""

    @classmethod
    @abstractmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]: ...

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.violations(comms, commander)


class UndatedStandingRule(StandingRule, declared_name=None):
    """A rule that cannot say when its condition began: it is broken on every read."""

    def broke_after(self, after: float) -> bool:
        return True


@dataclass(frozen=True)
class LostTools(UndatedStandingRule):
    thread: ThreadIncarnation
    changes: tuple[str, ...]

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        found = []
        for unit in observed:
            thread = unit.source.thread
            changes = thread.lost_tools()
            if changes:
                found.append(cls(thread.incarnation, changes))
        return tuple(found)

    def line(self) -> str:
        return f"{self.thread.name}: {'; '.join(self.changes)}"


@dataclass(frozen=True)
class HeldBranch:
    """One open order joined to its own allocated checkout. Only the branch difference reads
    that worktree; its unit may retire it mid-frame, so every later Git read goes through the
    order's repository, which owns the branch refs and remote heads and outlives the worktree."""

    todo: Todo
    holder: ThreadIncarnation
    difference: BranchDifference
    worktree: Path
    repository: PresentCheckout = field(compare=False, repr=False)
    source: UnitObservation = field(compare=False, repr=False)

    @property
    def problem(self) -> str:
        return self.todo.id


class HeldBranchRule(UndatedStandingRule, declared_name=None):
    """Branch rules share the heartbeat's existing progress frame and one scan."""

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return HeldBranchRule

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        storage = comms.registry.storage_rule
        branches = []
        for state in observed:
            for unit in state.readable_units():
                for todo in state.source.todos:
                    path = storage.order_checkout(unit.name, todo.id)
                    if path.is_dir():
                        branches.append(HeldBranch(
                            todo, unit, frame.pulls.branch_difference(path), path,
                            frame.pulls.checkout(Path(todo.order.checked().check.directory)),
                            state.source,
                        ))
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
class BranchGrowth(HeldBranchRule):
    """A held branch's source growth needs an explanation in its verdict."""

    problem: str
    holder: ThreadIncarnation
    repository: RepositoryName
    branch: str
    growth: int = field(compare=False)
    limit: ClassVar[int] = 40

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(
            cls(held.problem, held.holder, held.difference.repository,
                held.difference.branch, held.difference.code_growth)
            for held in branches if held.difference.code_growth > cls.limit
        )

    def line(self) -> str:
        return (
            f"SOURCE GROWTH: {self.problem} {self.holder.name} "
            f"{self.repository.to_wire()}:{self.branch} grows src by {self.growth} code lines "
            f"(limit {self.limit}) -> the verdict states why the growth is necessary, "
            "or the slop is deleted"
        )


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
    """A question or delivery awaiting the commander; each member owns what clears it
    and which actions it prevents while it waits."""

    message: Message

    def questions_for(self, unit: ThreadIncarnation) -> tuple[Message, ...]:
        """Only a unit's question belongs on that unit's observation."""
        return ()

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

    @abstractmethod
    def clearing_action(self) -> str:
        """The action that clears this member's debt, printed by the refusing verb."""

    def due(self, now: float) -> bool:
        return True

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        now = time.time()
        return tuple(rule for rule in cls.opened(comms, commander) if rule.due(now))

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return OwedAnswer

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        now = time.time()
        return tuple(rule for rule in frame.answers if isinstance(rule, cls) and rule.due(now))

    @classmethod
    def opened(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return cls.read_scan(comms, commander, HeldOrders.read(comms, commander))

    @classmethod
    def read_scan(cls, comms: Comms, commander: str, scan: HeldOrders) -> tuple[Self, ...]:
        # Capture committed order/directive references before the bus cut.
        snapshot = comms.registry.snapshot()
        requests = comms.goals.todos.open_requests(snapshot.require(commander).incarnation)
        held = scan.holders
        with comms.bus.bus.reading() as source:
            return tuple(rule for member in cls.members_with(cls)
                         for rule in member.read_open(source, snapshot, held, commander, requests))

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        """Whether an action naming these open messages answers this one."""
        return any(message.message_id == self.message.message_id for message in named)

    def blocks(self, reached: frozenset[str], action: CommanderVerb) -> bool:
        """An unanswered question must be answered before unrelated work."""
        return True

    def misaddressed(self, named: tuple[Message, ...], reached: frozenset[str]) -> str:
        """Why naming this debt in an action that reaches only these threads would record an
        answer its asker never receives; empty when it would not."""
        return ""


class AnswersOwedDegradation(Degradation, BlocksRelease):
    """Messages to the commander are waiting past their window; each message is one sign."""

    @classmethod
    def found(cls, comms: Comms, commander: str, frame: HeartbeatFrame) -> tuple[Sign, ...]:
        return tuple(sign for rule in OwedAnswer.scan_observed(comms, commander, (), frame)
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
              observed: Sequence[UnitState], frame: HeartbeatFrame) -> Self:
        return cls(why)

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        why = cls.reason(comms, commander)
        return (cls.found(why, comms, commander, observed, frame),) if why else ()

    def line(self) -> str:
        return self.text.format(why=self.why)


@dataclass(frozen=True)
class HeldOrderDue(DatedStandingRule, declared_name=None):
    """Each held order supplies one dated condition; the rule owns its wording."""

    problem: str
    holder: ThreadIncarnation
    order: str
    since: EventTime

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        """Conditions read the frame's own progress facts, never a second forge read."""
        return tuple(cls(todo.id, todo.held().owner_identity, todo.order.checked().id, due)
                     for todo, due in cls.conditions(comms, frame))

    @classmethod
    @abstractmethod
    def conditions(
        cls, comms: Comms, frame: HeartbeatFrame,
    ) -> Iterable[tuple[Todo, EventTime]]: ...

    def began(self) -> float:
        return self.since.recorded_at("An order alarm")


class ReviewTimerUp(HeldOrderDue):
    """Each round of a held order's review timer is its own condition, so the heartbeat wakes
    the commander once per round; the unit sees no timer and nothing is struck."""

    @classmethod
    def conditions(cls, comms: Comms, frame: HeartbeatFrame) -> Iterable[tuple[Todo, EventTime]]:
        return comms.goals.todos.timers_up_in(
            frame.todos, time.time(), frame.pulls.progress(frame.todos, comms.registry),
        )

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
        cls, comms: Comms, commander: str, observed: Sequence[UnitState], frame: HeartbeatFrame,
    ) -> tuple[Self, ...]:
        now = time.time()
        return tuple(
            rule
            for todo in frame.todos
            for claim, request in frame.pulls.claimed_heads(todo)
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
class UnreviewedHead(OrderRefusingStandingRule, DatedStandingRule):
    """One ready head, including an orderless PR, waiting too long for a recorded verdict."""
    request: PullRequestHead
    due_at: float
    seconds: ClassVar[int] = 10 * 60

    @classmethod
    def scan_reviews(cls, goals: Goals, commander: str, pulls: ProgressRead) -> tuple[Self, ...]:
        now = time.time()
        return tuple(
            cls(request, due)
            for repo in goals.registry.snapshot().commander_project.repository_names()
            for row in pulls.open(repo)
            for due in row.review_due(now, cls.seconds)
            for request in (PullRequestHead(repo, row.number, row.head),)
            if not goals.todos.head_reviews(request)
        )

    def began(self) -> float:
        return self.due_at

    def line(self) -> str:
        return (f"UNREVIEWED HEAD: {self.request.repo.to_wire()}#{self.request.pr} "
                f"head {self.request.head} -> judge ok/back --pr {self.request.pr} "
                f"--head {self.request.head}")


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
              observed: Sequence[UnitState], frame: HeartbeatFrame) -> HuntDue:
        since = cls.hunted(comms, commander) + cls.seconds
        readable = {unit for state in observed for unit in state.readable_units()}
        todos = tuple(todo for todo in frame.todos
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

    def clearing_action(self) -> str:
        return (f"Reply to {self.message.sender} in the session or on the bus, "
                f"or mark request {self.request.value} done.")

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
class BoardDisagrees(UndatedStandingRule):
    """An open order reads as no commit despite its linked pull request having merged."""

    problem: str
    order: str
    merge: str

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        from agent_comms.todos import ProgressSource

        progress = frame.pulls.progress(frame.todos, comms.registry)
        return tuple(
            cls(todo.id, todo.order.checked().id, merged.sha)
            for todo in frame.todos
            for merged in progress[todo.id].missing_merges(
                ProgressSource.of(todo, comms.registry, frame.pulls),
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
