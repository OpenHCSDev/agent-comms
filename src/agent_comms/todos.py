"""A durable todo owns its one assignment; it does not start an agent.

This is an opt-in post-PR1 primitive. An ACP/fork adapter must verify current
thread identities and reserve a todo BEFORE spawning a worker. File claims,
collaboration edges and thread goals remain separate authorities.
"""

from __future__ import annotations

import re
import sqlite3
import time
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from abc import abstractmethod
from typing import ClassVar

from .field_codec import FieldCodec
from .command import Command
from .declared_family import DeclaredFamily
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .threads import Thread
from .typed_table import Column, TypedStore, TypedTable
from .recorded_time import EventTime, UnrecordedEventTime
from .change_report import FileLine
from .goals import OrderCheck
from .child_process import BoundedRun, ExitedOutcome


@dataclass(frozen=True, kw_only=True)
class OrderDelivery(DeclaredFamily, affix="Delivery"):
    at: EventTime = field(default_factory=UnrecordedEventTime)

    async def run(self, check, *command):
        result = await BoundedRun.run(tuple(command), cwd=check.directory, timeout=120)
        if result.outcome != ExitedOutcome(0):
            raise ValueError(result.stderr.decode(errors="replace") or "Delivery source check failed.")
        return result.stdout.decode(errors="replace")

    async def verified(self, check):
        await self.require_source(check)
        result = await BoundedRun.run(("bash", "-c", check.command), cwd=check.directory, timeout=120)
        check.result.require(result)
        return replace(self, at=await self.delivery_time(check))

    @abstractmethod
    async def require_source(self, check): ...

    @abstractmethod
    async def delivery_time(self, check): ...


@dataclass(frozen=True, kw_only=True)
class RecordedCommitDelivery(OrderDelivery, declared_name="commit"):
    """A delivery recorded before checked and delivered commits were told apart.

    Its one commit is both facts; Core refuses new deliveries of this kind."""
    commit: str

    def __post_init__(self) -> None:
        if any(not isinstance(sha, str) or not 7 <= len(sha) <= 40
               or sha.strip("0123456789abcdef") for sha in (self.commit, self.delivered)):
            raise ValueError("Delivery requires a checked and a delivered commit hash.")

    @property
    def delivered(self) -> str:
        return self.commit

    async def require_source(self, check: OrderCheck) -> None:
        raise ValueError("A commit delivery names the unit's delivered commit and the checked one.")

    async def delivery_time(self, check: OrderCheck) -> EventTime:
        return EventTime.capture(float(
            await self.run(check, "git", "show", "-s", "--format=%ct", self.delivered)))


@dataclass(frozen=True, kw_only=True)
class CommitDelivery(RecordedCommitDelivery, declared_name="delivered_commit"):
    """The check runs on ``commit``; ``delivered_commit`` is the unit's pushed ancestor of it."""
    delivered_commit: str

    @property
    def delivered(self) -> str:
        return self.delivered_commit

    async def require_source(self, check: OrderCheck) -> None:
        head = (await self.run(check, "git", "rev-parse", "HEAD", f"{self.commit}^{{commit}}",
                               f"{self.delivered}^{{commit}}")).split()
        dirty = await self.run(check, "git", "status", "--porcelain", "--untracked-files=no")
        if len(head) != 3 or head[0] != head[1] or dirty.strip():
            raise ValueError("Run the order check on its clean checked commit.")
        base = await self.run(check, "git", "merge-base", self.delivered, self.commit)
        if base.strip() != head[2]:
            raise ValueError(
                f"Delivered commit {self.delivered} is not in the checked commit {self.commit}.")
        pushed = await self.run(check, "git", "branch", "--remotes", "--contains", self.delivered,
                                "--list", "origin/*")
        if not pushed.strip():
            raise ValueError(
                f"Delivered commit {self.delivered} is on no origin branch; push it first.")


@dataclass(frozen=True, kw_only=True)
class OperationDelivery(OrderDelivery):
    async def require_source(self, check):
        pass

    async def delivery_time(self, check):
        return EventTime.capture(time.time())


@dataclass(frozen=True)
class FirstCall(DeclaredFamily, affix="FirstCall"):
    def observe(self):
        return self

    def notify(self):
        return self


class AwaitingFirstCall(FirstCall):
    def observe(self):
        return ObservedFirstCall()

    def notify(self):
        return NotifiedFirstCall()


class ObservedFirstCall(FirstCall):
    pass


class NotifiedFirstCall(FirstCall):
    def observe(self):
        return LateObservedFirstCall()


class LateObservedFirstCall(ObservedFirstCall, NotifiedFirstCall):
    pass


class FirstCallEvent(DeclaredFamily, affix="FirstCallEvent"):
    @classmethod
    @abstractmethod
    def apply(cls, current): ...


class ObservedFirstCallEvent(FirstCallEvent):
    @classmethod
    def apply(cls, current):
        return current.observe()


class NotifiedFirstCallEvent(FirstCallEvent):
    @classmethod
    def apply(cls, current):
        return current.notify()


class OrderWords(str):
    """Text the commander writes; quoted, backticked, indented and path-like spans are someone else's words."""

    CITED: ClassVar[re.Pattern] = re.compile(
        r'(?m)^ {4}.*$|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|\S*/\S*|\S+\.(?:py|md|jsonl?|txt|png|toml|ts|log|patch)\b')
    REFUSED: ClassVar[re.Pattern]
    refusal: ClassVar[str]
    least_words: ClassVar[int] = 1

    def __new__(cls, text: str):
        if refused := cls.refused(text):
            raise ValueError(refused)
        return super().__new__(cls, text)

    @classmethod
    def refused(cls, text: str) -> str | None:
        found = cls.found(text)
        if len(text.split()) < cls.least_words or cls.refuses(found):
            return cls.refusal.format(cls.least_words, found)
        return None

    @staticmethod
    def refuses(found: list[str]) -> bool:
        return bool(found)

    @classmethod
    def found(cls, text: str) -> list[str]:
        return sorted({m.group(0).lower() for m in cls.REFUSED.finditer(cls.CITED.sub(' ', text))})


class PlainText(OrderWords):
    """Prose in the words the user uses: no word that hides what happened."""

    REFUSED = re.compile(r'\b(custody|admission|admit\w*|receipt\w*|checkpoint\w*|provenance|authority|authorities|witness\w*|lattice|surface|disposition\w*|invariant\w*|semantic\w*|ownership|lifecycle|acceptance|canonical|retained|borrow(?:ed|er|ers)?|bounded|certif\w*|recertif\w*|candidate|fence\w*|sidecar\w*|substrate|installed-path|source[- ]paired|exact[- ]head|integration owner|separately owned|follow-?up|remaining scope|not claimed|remains held|load[- ]bearing|cutover|carr(?:y|ies) across|declared dependency|qualifying)\b', re.I)
    refusal = "Jargon {1} hides what happened; say it plainly."


class StrikeReason(PlainText):
    """What a unit did, plainly, in enough words to stand on the public record."""

    refusal = "Say what the unit did plainly in {}+ words; jargon {} hides what happened."
    least_words = 10


class OrderText(OrderWords):
    """An order states the edit; it never hands the decision back or asks permission."""

    REFUSED = re.compile(r"\b(please|could you|would you|can you|if possible|consider|maybe|perhaps|when you get a chance|"
                         r"feel free|if you (think|agree|want)|thoughts\??|what do you think|thanks|thank you|sorry|"
                         r"no rush|optional(ly)?|up to you|might want|suggest)\b", re.I)
    refusal = "Soft wording {1}: state the edit, do not ask."


class BluntText(OrderWords):
    """Witnessed incompetence is named as such: the text must call something stupid, slop or wrong."""

    REFUSED = re.compile(r"\b(stupid|slop|bullshit|useless|wrong|lazy|garbage|nonsense|incompeten\w*|sloppy|theater|dumb|"
                         r"worthless|broke|broken|pointless|waste)\b", re.I)
    refusal = "Not blunt: name what was stupid/slop/wrong and why, or pass --clean if nothing was."

    @staticmethod
    def refuses(found: list[str]) -> bool:
        return not found


class FairText(OrderWords):
    """Paper 1 proves multiple parents derive every required pair with nothing outside, which composition and
    forwarding supply outside (Fig 3, Thm 4.6): faulting inheritance itself is dogma, not a restated fact.
    A Banned: line may name it."""

    CITED = re.compile(OrderWords.CITED.pattern + r'|^Banned:.*$')
    REFUSED = re.compile(r"composition over inheritance|(prefer|favou?r|use) composition|inherits (two|both|multiple|several|\d+)\b"
                         r"|multiple (inheritance|parents) (is (bad|wrong|dangerous|confusing|fragile|slop)|causes|creates|smells|complicates)"
                         r"|diamond problem|fragile base|(avoid|no|stop using|drop) (inheritance|mixins?|multiple (parents|inheritance))"
                         r"|too many (parents|base classes|mixins)|deep (inheritance|hierarch)|mixing (of )?\w+ and \w+"
                         r"|inheritance (abuse|soup|hell|is wrong)", re.I)
    refusal = ("{1} faults inheritance itself. Name the restated fact in a Slop: line (identity, membership, "
               "implementation or state) and the declaration that derives it.")


class HierarchyText(OrderWords):
    """A forwarder restates an implementation pair, so the cure is the hierarchy, never a route: text may not keep
    a forwarder, and a Banned: item may forbid removing classes, bases or parents but not adding them."""

    REFUSED = re.compile(r"(leave|keep|retain|preserve)[^.\n]{0,60}\bforward|forwarders? (stay|remain)|reach(es|ing)? through", re.I)
    BANS: ClassVar[re.Pattern] = re.compile(r"\b(new class(es)?|new base|base class|changing the hierarchy|inheritance|mixins?|"
                                            r"multiple parents|changing \w+State)\b", re.I)
    UNDOES: ClassVar[re.Pattern] = re.compile(r"\b(flatten\w*|remov\w*|replac\w*|delet\w*|collaps\w*|single-parent|one-parent|merg\w*)\b", re.I)
    refusal = ("{1} keeps a forwarder or bans fixing the hierarchy: fold the false owner into the real one or compose "
               "providers as parents, and delete every route.")

    @classmethod
    def found(cls, text: str) -> list[str]:
        banned = re.findall(r'(?m)^Banned:(.*)$', cls.CITED.sub(' ', text))
        return super().found(text) + [item.strip() for line in banned for item in re.split(r',|;|\bor\b', line)
                                      if cls.BANS.search(item) and not cls.UNDOES.search(item)]


@dataclass(frozen=True, kw_only=True)
class ProblemOrder(DeclaredFamily, affix="ProblemOrder"):
    """One checked order lives on its Todo, independently of the unit's Goal."""

    id: str
    owner: FileLine
    check: OrderCheck
    edits: tuple[str, ...]
    banned: tuple[str, ...]
    report: str
    at: EventTime
    first_call_deadline: EventTime
    first_call: FirstCall = field(default_factory=AwaitingFirstCall)
    check_corrections: tuple[CorrectCheckTodo, ...] = field(
        default=(), metadata={"wire_omit_default": True})

    def __post_init__(self):
        if not self.edits or not self.banned:
            raise ValueError("An order requires edits and banned actions.")
        if any(not text.strip() for text in (self.id, self.report, *self.edits, *self.banned)):
            raise ValueError("Order text cannot be empty.")
        for text in (self.report, *self.edits):
            for family in (OrderText, FairText, HierarchyText):
                family(text)
        self.at.require_recorded("An order")
        self.first_call_deadline.require_deadline()

    pending: ClassVar[bool]

    def copy_as(self, kind, **evidence):
        return kind(**{item.name: getattr(self, item.name) for item in fields(self)}, **evidence)

    def instructions(self, todo):
        return "\n".join((f"Problem: {todo.id}: {todo.text}", f"Order: {self.id}",
            f"Owner: {self.owner.to_wire()}",
            f"Check: {self.check.directory} :: {self.check.command}",
            f"Deadline: {FieldCodec.encode(todo.deadline)}",
            *(f"{n}. {edit}" for n, edit in enumerate(self.edits, 1)),
            "Banned: " + "; ".join(self.banned), "Report: " + self.report))

    def delivered(self, delivery):
        delivery.at.require_delivery()
        return self.copy_as(DeliveredProblemOrder, delivery=delivery)

    def corrected(self, correction: CorrectCheckTodo) -> ProblemOrder:
        if correction.previous != self.check:
            raise ValueError("The order's check changed since it was read; read it again.")
        return replace(self, check=correction.check,
                       check_corrections=(*self.check_corrections, correction))

    def installed(self, works, artifact):
        raise ValueError("Check the delivered order before recording installation.")

    def require_close(self):
        raise ValueError("Closing requires recorded installed application evidence.")


@dataclass(frozen=True, kw_only=True)
class PendingProblemOrder(ProblemOrder):
    pending: ClassVar[bool] = True


@dataclass(frozen=True, kw_only=True)
class DeliveredProblemOrder(ProblemOrder):
    delivery: OrderDelivery
    pending: ClassVar[bool] = False

    def delivered(self, delivery):
        if delivery != self.delivery:
            raise ValueError("The order already records a different delivery.")
        return self

    def corrected(self, correction: CorrectCheckTodo) -> ProblemOrder:
        raise ValueError("A delivered order keeps the check its delivery passed.")

    def installed(self, works, artifact):
        return self.copy_as(InstalledProblemOrder, works=works, artifact=artifact)


@dataclass(frozen=True, kw_only=True)
class InstalledProblemOrder(DeliveredProblemOrder):
    works: str
    artifact: str

    def __post_init__(self):
        super().__post_init__()
        if len(self.works.split()) < 8 or not self.artifact.strip():
            raise ValueError("Describe what worked (8+ words) and name its artifact file.")

    def installed(self, works, artifact):
        if (works, artifact) != (self.works, self.artifact):
            raise ValueError("The order already records different installed evidence.")
        return self

    def require_close(self):
        pass


@dataclass(frozen=True)
class Hunt(DeclaredFamily, affix="Hunt"):
    where: FileLine
    why: str
    at: EventTime

    def __post_init__(self):
        if len(self.why.split()) < 10:
            raise ValueError("Describe the inspected code and verdict in ten or more words.")
        self.at.require_recorded("A hunt")

    def require_order(self, order):
        pass


@dataclass(frozen=True)
class CleanHunt(Hunt):
    pass


@dataclass(frozen=True)
class OrderedHunt(Hunt):
    sent: str

    def require_order(self, order):
        if self.sent != order.id:
            raise ValueError("The hunt must name this problem's current order.")


class TodoError(ValueError):
    """A todo request is invalid or its persisted state cannot be trusted."""


class TodoConflict(TodoError):  # noqa: N818 - domain-specific conflict outcome
    """The expected revision, assignee or generation is no longer current."""

    def __init__(self, current: Todo):
        self.current = current
        owner = current.assignment.owner if current.assignment else "unassigned"
        super().__init__(f"Todo {current.id} changed (revision {current.revision}, owner {owner}).")


@dataclass(frozen=True, slots=True)
class GoalRef:
    owner: str
    owner_created: float
    goal_id: str

    def __post_init__(self) -> None:
        _text(self.owner, "Thread name", 128)
        try:
            self.incarnation.require_recorded()
        except ValueError as error:
            raise TodoError("Goal reference requires its recorded owner incarnation") from error
        _text(self.goal_id, "Goal ID", 128)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created)


@dataclass(frozen=True, slots=True)
class Assignment:
    owner: str
    owner_created: float
    parent: str
    parent_created: float
    generation: str

    def __post_init__(self) -> None:
        _text(self.owner, "Thread name", 128)
        _text(self.parent, "Thread name", 128)
        try:
            self.owner_identity.require_recorded()
            self.parent_identity.require_recorded()
        except ValueError as error:
            raise TodoError("Assignment requires recorded owner and parent incarnations") from error
        _text(self.generation, "Assignment generation", 128)

    @property
    def owner_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created)

    @property
    def parent_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.parent, self.parent_created)

    def owned_by(self, actor: ThreadIncarnation, generation: str | None) -> bool:
        return (self.owner_identity, self.generation) == (actor, generation)

    def require_successor(self, proposed: Assignment, current: Todo) -> None:
        if self.generation == proposed.generation:
            raise TodoConflict(current)


class TodoState(DeclaredFamily, affix="TodoState"):
    open: ClassVar[bool] = True  # on the board until closed with installed evidence

    @classmethod
    def prepare_change(cls, goals, current):
        pass

    @classmethod
    def assign(cls, current: Todo, proposed: Assignment) -> Todo:
        raise TodoConflict(current)

    @classmethod
    def change(cls, current: Todo, command: ChangeTodoState) -> Todo:
        return command.state.decide(current, command)

    @classmethod
    def decide(cls, current: Todo, command: ChangeTodoState) -> Todo:
        current.require_actor(command.actor, command.generation)
        return (
            current
            if cls is current.state
            else replace(current, state=cls, revision=current.revision + 1)
        )


class OpenTodoState(TodoState):
    @classmethod
    def assign(cls, current, proposed):
        return current.assigned(proposed)


class BlockedTodoState(TodoState):
    pass


class DoneTodoState(TodoState):
    open: ClassVar[bool] = False

    @classmethod
    def prepare_change(cls, goals, current):
        current.order.require_close()

    @classmethod
    def change(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def decide(cls, current, command):
        current.require_commander(command.actor)
        return replace(current, state=cls, assignment=None, revision=current.revision + 1)


@dataclass(frozen=True)
class TodoOperation(Command, DeclaredFamily, affix="TodoOperation"):
    expected_revision: int

    def __post_init__(self) -> None:
        if FieldCodec.decode(int, self.expected_revision) <= 0:
            raise TodoError("Todo operation requires a positive exact revision")

    def prepare(self, goals, current):
        return self

    @abstractmethod
    def apply(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    proposed: Assignment
    deadline: EventTime
    order: PendingProblemOrder

    def apply(self, current):
        self.deadline.require_deadline()
        if (current.assignment, current.deadline, current.order) == (self.proposed, self.deadline, self.order):
            return current
        current.require_revision(self.expected_revision)
        current.require_commander(self.proposed.parent_identity)
        assigned = current if current.assignment == self.proposed else current.state.assign(current, self.proposed)
        return replace(assigned, deadline=self.deadline, order=self.order, revision=current.revision + 1)


@dataclass(frozen=True)
class StrikeTodo(TodoOperation):
    actor: ThreadIncarnation
    previous: Assignment
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.require_assignment(self.previous)
        _text(self.reason, "Strike reason", 2000)
        StrikeReason(self.reason)
        return replace(current, strikes=(*current.strikes, self), revision=current.revision + 1)


@dataclass(frozen=True)
class VoidStrikeTodo(TodoOperation):
    """The commander takes back a strike recorded in error; the strike stays on the record."""
    actor: ThreadIncarnation
    strike: StrikeTodo
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        if self.strike not in current.live_strikes(self.strike.previous.owner):
            raise TodoConflict(current)
        _text(self.reason, "Void reason", 2000)
        StrikeReason(self.reason)
        return replace(current, voided=(*current.voided, self), revision=current.revision + 1)


@dataclass(frozen=True)
class CorrectCheckTodo(TodoOperation):
    """The commander replaces a wrong check of an open order.

    The order keeps its id, holder, deadline and strikes; a correction is not a resend."""
    actor: ThreadIncarnation
    previous: OrderCheck
    check: OrderCheck
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        _text(self.reason, "Check correction reason", 2000)
        return replace(current, order=current.order.corrected(self), revision=current.revision + 1)


@dataclass(frozen=True)
class RecordFirstCallTodo(TodoOperation):
    actor: ThreadIncarnation
    event: type[FirstCallEvent]

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        order = replace(current.order, first_call=self.event.apply(current.order.first_call))
        return current if order == current.order else replace(current, order=order, revision=current.revision + 1)


@dataclass(frozen=True)
class RecordHuntTodo(TodoOperation):
    actor: ThreadIncarnation
    hunt: Hunt

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        self.hunt.where.require_exists()
        self.hunt.require_order(current.order)
        return replace(current, hunts=(*current.hunts, self.hunt), revision=current.revision + 1)


@dataclass(frozen=True)
class DeliverTodo(TodoOperation):
    actor: ThreadIncarnation
    delivery: OrderDelivery

    def prepare(self, goals, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(self, delivery=asyncio.run(self.delivery.verified(current.order.check)))

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(current, order=current.order.delivered(self.delivery), revision=current.revision + 1)


@dataclass(frozen=True)
class InstallTodo(TodoOperation):
    actor: ThreadIncarnation
    works: str
    artifact: str

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        if not Path(self.artifact).is_file():
            raise ValueError("Retain the installed application artifact before closing.")
        installed = replace(current, order=current.order.installed(self.works, self.artifact))
        return DoneTodoState.decide(installed, ChangeTodoState(
            current.revision, DoneTodoState, self.actor, current.assignment.generation))


@dataclass(frozen=True)
class ChangeTodoState(TodoOperation):
    state: type[TodoState]
    actor: ThreadIncarnation
    generation: str | None

    def prepare(self, goals, current):
        self.state.prepare_change(goals, current)
        return self

    def apply(self, current):
        current.require_revision(self.expected_revision)
        return current.state.change(current, self)


@dataclass(frozen=True, slots=True)
class Todo(TypedTable):
    id: str = field(metadata={"sql": Column(primary_key=True)})
    repo: str
    text: str
    creator: str
    creator_created: float
    state: type[TodoState]
    revision: int = field(metadata={"sql": Column(check="revision>0")})
    goal: GoalRef | None
    assignment: Assignment | None
    order: ProblemOrder
    deadline: EventTime = field(default_factory=UnrecordedEventTime)
    strikes: tuple[StrikeTodo, ...] = ()
    hunts: tuple[Hunt, ...] = ()
    voided: tuple[VoidStrikeTodo, ...] = ()

    @property
    def pending(self):
        return self.order.pending

    @property
    def creator_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.creator, self.creator_created)

    @property
    def strike_count(self) -> int:
        return 0 if self.assignment is None else len(self.live_strikes(self.assignment.owner))

    def live_strikes(self, unit: str | None = None) -> tuple[StrikeTodo, ...]:
        """The strikes still counting on this task, against ``unit`` on any order of it."""
        taken_back = tuple(void.strike for void in self.voided)
        return tuple(recorded for recorded in self.strikes
                     if unit in (None, recorded.previous.owner) and recorded not in taken_back)

    def require_result(self, result: Todo) -> None:
        if self != result:
            raise TodoConflict(self)

    def require_revision(self, expected: int) -> None:
        if self.revision != expected:
            raise TodoConflict(self)

    def require_assignment(self, expected: Assignment | None) -> None:
        if self.assignment != expected:
            raise TodoConflict(self)

    def require_assignee(self, actor: ThreadIncarnation, generation: str | None) -> None:
        if self.assignment is None or not self.assignment.owned_by(actor, generation):
            raise TodoConflict(self)

    def assigned(self, proposed: Assignment) -> Todo:
        if self.assignment is not None:
            self.assignment.require_successor(proposed, self)
        return replace(self, assignment=proposed, revision=self.revision + 1)

    def require_released_order(self, owner: ThreadIncarnation, goal_id: str) -> None:
        if self.assignment is not None and self.assignment.owned_by(owner, goal_id):
            raise TodoConflict(self)

    def require_commander(self, actor: ThreadIncarnation) -> None:
        if self.creator_identity != actor:
            raise TodoConflict(self)

    def require_actor(self, actor: ThreadIncarnation, generation: str | None) -> None:
        if self.creator_identity != actor:
            self.require_assignee(actor, generation)

    def require_same_creation(self, proposed: Todo) -> None:
        self.require_result(
            replace(
                self,
                id=proposed.id,
                repo=proposed.repo,
                text=proposed.text,
                creator=proposed.creator,
                creator_created=proposed.creator_created,
                goal=proposed.goal,
                deadline=proposed.deadline,
                order=proposed.order,
            )
        )


def _text(value: str, label: str, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise TodoError(f"{label} must be nonempty and at most {limit} characters.")
    return value


def _thread(thread: Thread) -> ThreadIncarnation:
    thread.role.require_executable(
        TodoError("Assignments require an executable registered thread snapshot.")
    )
    return _participant(thread)


def _participant(thread: Thread) -> ThreadIncarnation:
    _text(thread.name, "Thread name", 128)
    try:
        thread.incarnation.require_recorded()
    except ValueError as error:
        raise TodoError("Creator requires a recorded incarnation") from error
    return thread.incarnation


def _repo(value: str) -> str:
    _text(value, "Repo ID", 200)
    pieces = value.split("/")
    if len(pieces) != 2 or any(
        piece in {"", ".", ".."} or not all(char.isalnum() or char in "-_." for char in piece)
        for piece in pieces
    ):
        raise TodoError("Repo ID must have the exact owner/repo form.")
    return value.lower()


@dataclass(frozen=True, slots=True)
class TodoStore(TypedStore):
    """One SQLite row owns both todo state and its exclusive assignment.

    Assignment identity and revision deduplicate known operations. An uncertain
    input is not permission to replay it; inspect its original retained result.
    This store never launches a child.
    """

    root: Path
    filename: ClassVar[str] = "todos.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (Todo,)

    def _select(self, where: str, parameters: tuple) -> tuple[Todo, ...]:
        """Rows in creation order; an absent store has none, and a read never creates one."""
        if not self.path.exists():
            return ()
        with self.snapshot() as db:
            return self._rows(db, where, parameters)

    @staticmethod
    def _rows(db: sqlite3.Connection, where: str, parameters: tuple) -> tuple[Todo, ...]:
        return tuple(Todo.read(db.execute(
            f"SELECT * FROM {Todo.declared_name} WHERE {where} ORDER BY rowid", parameters)))

    @classmethod
    def _one(cls, rows: tuple[Todo, ...], todo_id: str) -> Todo:
        if not rows:
            raise TodoError(f"Unknown todo: {todo_id}.")
        return rows[0]

    @classmethod
    def _current(cls, db: sqlite3.Connection, todo_id: str) -> Todo:
        return cls._one(cls._rows(db, "id=?", (todo_id,)), todo_id)

    def get(self, todo_id: str) -> Todo:
        _text(todo_id, "Todo ID", 128)
        return self._one(self._select("id=?", (todo_id,)), todo_id)

    def list(self, repo: str) -> tuple[Todo, ...]:
        return self._select("repo=?", (_repo(repo),))

    def for_goal(self, owner: ThreadIncarnation, goal_id: str) -> tuple[Todo, ...]:
        return self._select(
            "json_extract(assignment,'$.owner')=? AND json_extract(assignment,'$.owner_created')=? "
            "AND json_extract(assignment,'$.generation')=?", (owner.name, owner.created_at, goal_id))

    def for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._select("creator=? AND creator_created=?", (commander.name, commander.created_at))

    def create(
        self,
        todo_id: str,
        repo: str,
        text: str,
        *,
        creator: Thread,
        owner: Thread,
        generation: str,
        deadline: EventTime,
        order: PendingProblemOrder,
        goal: GoalRef | None = None,
    ) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 2000)
        repo_id = _repo(repo)
        identity, assignee = _participant(creator), _thread(owner)
        deadline.require_deadline()
        if goal is not None and type(goal) is not GoalRef:
            raise TodoError("Goal reference must be a typed GoalRef.")
        proposed = Todo(
            id=todo_id,
            repo=repo_id,
            text=text,
            creator=identity.name,
            creator_created=identity.created_at,
            state=OpenTodoState,
            revision=1,
            goal=goal,
            assignment=Assignment(assignee.name, assignee.created_at,
                                  identity.name, identity.created_at, generation),
            deadline=deadline,
            order=order,
        )
        with self.writing() as db:
            proposed.insert_if_absent(db)
            current = self._current(db, todo_id)
            current.require_same_creation(proposed)
            current.require_assignment(proposed.assignment)
            return current

    def apply(self, todo_id: str, command: TodoOperation) -> Todo:
        with self.writing() as db:
            current = self._current(db, todo_id)
            result = command.apply(current)
            if result == current:
                return current
            result.upsert(db)
            return self._current(db, todo_id)

    def assign(
        self,
        todo_id: str,
        *,
        expected_revision: int,
        owner: Thread,
        parent: Thread,
        generation: str,
        deadline: EventTime,
        order: PendingProblemOrder,
    ) -> Todo:
        identity, parent_identity = _thread(owner), _thread(parent)
        proposed = Assignment(
            identity.name,
            identity.created_at,
            parent_identity.name,
            parent_identity.created_at,
            generation,
        )
        return self.apply(todo_id, AssignTodo(expected_revision, proposed, deadline, order))
