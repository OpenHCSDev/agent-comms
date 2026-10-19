"""A durable todo owns its one assignment; it does not start an agent.

This is an opt-in post-PR1 primitive. An ACP/fork adapter must verify current
thread identities and reserve a todo BEFORE spawning a worker. File claims,
collaboration edges and thread goals remain separate authorities.
"""

from __future__ import annotations

import calendar
import json
import re
import subprocess
import sqlite3
import time
import asyncio
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, Self
from uuid import uuid4

from .change_report import FileLine
from .child_process import BoundedRun, ChildResult, ExitedOutcome
from .command import Command
from .declared_family import DeclaredFamily
from .field_codec import (
    FieldCodec, GitCommitText, MalformedRecord, NullMember, RecordMember, SubclassUnion, WireValue,
    boundary_decoder,
)
from .channel_targets import CommandTarget
from .goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from .recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .threads import Thread
from .typed_table import Column, Index, JsonStorage, TypedStore, TypedTable
from .storage_rules import StorageRule
from .plan_phases import OrderPhase, UnplannedPhase

if TYPE_CHECKING:
    from .goal_management import Goals


@dataclass(frozen=True, kw_only=True)
class OrderDelivery(DeclaredFamily, affix="Delivery"):
    at: EventTime = field(default_factory=UnrecordedEventTime)

    @property
    def work(self) -> str:
        return "delivered work"

    async def run(self, check, *command):
        result = await BoundedRun.run(tuple(command), cwd=check.directory, timeout=120)
        if result.outcome != ExitedOutcome(0):
            raise ValueError(result.stderr.decode(errors="replace") or "Delivery source check failed.")
        return result.stdout.decode(errors="replace")

    async def verified(self, check: OrderCheck, deletes: OrderDeletion) -> OrderDelivery:
        """The order's check and every count its deletion declares run on the delivered source."""
        for each in (check, *deletes.checks):
            await self.require_source(each)
            each.result.require(await self.checked(each))
        return replace(self, at=await self.delivery_time(check))

    @abstractmethod
    async def require_source(self, check): ...

    @abstractmethod
    async def checked(self, check: OrderCheck) -> ChildResult: ...

    @abstractmethod
    async def delivery_time(self, check): ...


@dataclass(frozen=True, kw_only=True)
class RecordedCommitDelivery(OrderDelivery, declared_name="commit"):
    """A delivery recorded before checked and delivered commits were told apart.

    Its one commit is both facts; Core refuses new deliveries of this kind."""
    commit: str

    def __post_init__(self) -> None:
        GitCommitText.decode(self.commit)
        GitCommitText.decode(self.delivered)

    @property
    def delivered(self) -> str:
        return self.commit

    @property
    def work(self) -> str:
        return f"delivered commit {self.delivered}"

    async def require_source(self, check: OrderCheck) -> None:
        raise ValueError("A commit delivery names the unit's delivered commit and the checked one.")

    async def checked(self, check: OrderCheck) -> ChildResult:
        return await asyncio.to_thread(check.at, self.commit, check.directory)

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
        delivered = (await self.run(
            check, "git", "rev-parse", f"{self.delivered}^{{commit}}")).strip()
        base = await self.run(check, "git", "merge-base", self.delivered, self.commit)
        if base.strip() != delivered:
            raise ValueError(
                f"Delivered commit {self.delivered} is not in the checked commit {self.commit}.")
        parents = await self.run(check, "git", "rev-list", "--parents", "-n", "1", self.delivered)
        if len(parents.split()) > 2:  # a main merge is never the unit's answer, only its arrival
            raise ValueError(f"Delivered commit {self.delivered} is a merge; deliver the unit's "
                             "first pushed commit that answers the order.")
        pushed = await self.run(check, "git", "branch", "--remotes", "--contains", self.delivered,
                                "--list", "origin/*")
        if not pushed.strip():
            raise ValueError(
                f"Delivered commit {self.delivered} is on no origin branch; push it first.")


@dataclass(frozen=True, kw_only=True)
class OperationDelivery(OrderDelivery):
    async def require_source(self, check):
        pass

    async def checked(self, check: OrderCheck) -> ChildResult:
        return await check.run()

    async def delivery_time(self, check):
        return EventTime.capture(time.time())


@dataclass(frozen=True)
class PullRequest:
    number: int
    branch: str
    head: str
    merge: str | None = None
    merged_at: float | None = None
    updated_at: float = 0.0
    orders: tuple[str, ...] = ()

    @classmethod
    @boundary_decoder
    def listed(cls, number: int, branch: str, head: str, merge: str, merged: str,
               updated: str, body: str) -> Self:
        """One row of ``gh pr list``; GitHub leaves an unmerged PR's merge commit and time empty."""
        return cls(int(number), branch, head, merge or None,
                   _github_time(merged) if merged else None, _github_time(updated),
                   ProblemOrder.pull_request_ids(body))


def _github_time(text: str) -> float:
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


class PullRequests:
    """Pull requests queried by immutable order identity, cached within one board read."""

    def __init__(self, todos: Sequence[Todo] = ()) -> None:
        self._read: dict[tuple[str, str, str], tuple[PullRequest, ...]] = {}
        self._orders: dict[str, dict[str, ProblemOrder]] = {}
        for todo in todos:
            if todo.holder:
                self._orders.setdefault(todo.repo, {})[todo.order.id] = todo.order

    def listed(self, repo: str, state: str, order: ProblemOrder) -> tuple[PullRequest, ...]:
        key = (repo, state, order.id)
        if key not in self._read:
            self._read[key] = self.read(repo, state, order)
        return self._read[key]

    def read(self, repo: str, state: str, order: ProblemOrder) -> tuple[PullRequest, ...]:
        orders = self._orders.setdefault(repo, {})
        orders[order.id] = order
        aliases = {f"o{index}": candidate for index, candidate in enumerate(orders.values())}
        selections = []
        for alias, candidate in aliases.items():
            search = json.dumps(f'repo:{repo} is:pr is:{state} in:body '
                                f'"{candidate.pull_request_reference}" sort:updated-desc')
            selections.append(
                f'{alias}: search(query: {search}, type: ISSUE, first: 100) {{ nodes {{ '
                '... on PullRequest { number headRefName headRefOid body '
                'mergeCommit { oid } mergedAt updatedAt } } }')
        listed = subprocess.run(
            ("gh", "api", "graphql", "-f", "query=query {" + " ".join(selections) + "}",
             "--jq", '.data | map_values(.nodes | map([.number, .headRefName, .headRefOid,'
             ' .mergeCommit.oid // "", .mergedAt // "", .updatedAt, .body]))'),
            capture_output=True, text=True, timeout=60)
        if listed.returncode:
            raise ValueError(listed.stderr.strip() or f"GitHub order lookup in {repo} failed.")
        rows = FieldCodec.decode(dict[str, tuple[tuple[int, str, str, str, str, str, str], ...]],
                                 FieldCodec.parse(listed.stdout))
        for alias, candidate in aliases.items():
            self._read[repo, state, candidate.id] = tuple(
                PullRequest.listed(*row) for row in rows[alias])
        return self._read[repo, state, order.id]


def _clock(at: float) -> str:
    return time.strftime("%H:%M", time.localtime(at))


@dataclass(frozen=True)
class DistanceReading:
    at: float
    distance: int


@dataclass(frozen=True)
class DistanceReadings:
    """The order's last two distance readings, oldest first: its base at the order, then its
    newest commit. Fewer than two read, or a last reading at done, project nothing."""
    readings: tuple[DistanceReading, ...] = ()

    @property
    def trend(self) -> DistanceTrend:
        """Velocity across the two readings: falling projects the finish, else it is flat."""
        last = self.readings[-2:]
        if len(last) < 2 or not last[1].distance:
            return UnreadTrend()
        first, final = last
        if final.distance >= first.distance:
            return FlatTrend(final.distance, final.at)
        rate = (first.distance - final.distance) / max(final.at - first.at, 1.0)
        return MovingTrend(final.distance, final.at + final.distance / rate)


class DistanceTrend(DeclaredFamily, affix="Trend"):
    """What the order's distance readings say about its finish."""

    @property
    @abstractmethod
    def projection(self) -> str: ...


@dataclass(frozen=True)
class MovingTrend(DistanceTrend):
    distance: int
    done: float

    @property
    def projection(self) -> str:
        return f"; distance {self.distance}, projected done {_clock(self.done)}"


@dataclass(frozen=True)
class FlatTrend(DistanceTrend):
    """The distance did not fall from the reading before ``since``."""

    distance: int
    since: float

    @property
    def projection(self) -> str:
        return f"; distance {self.distance}, not falling"


@dataclass(frozen=True)
class UnreadTrend(DistanceTrend):
    """Fewer than two readings, or the last one at done: nothing to project from."""

    @property
    def projection(self) -> str:
        return ""


@dataclass(frozen=True)
class ProgressSource:
    """What an order's progress is read from: its unit's checkout and the repository's PRs."""
    todo: Todo
    checkout: Path
    pulls: PullRequests

    @property
    def since(self) -> float:
        return self.todo.order.at.recorded_at("An order")

    def requests(self, state: str) -> list[PullRequest]:
        """The order's explicit PR references survive checkout removal or branch reuse."""
        return [pr for pr in self.pulls.listed(self.todo.repo, state, self.todo.order)
                if self.todo.order.id in pr.orders and pr.updated_at >= self.since]

    def commits(self, *excluded: str) -> Iterator[tuple[str, float]]:
        """The checkout's newest commit since the order that no ``excluded`` ref already holds."""
        after = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.since))
        log = subprocess.run(
            ("git", "-C", str(self.checkout), "log", "-1", f"--since={after}",
             "--format=%H %ct", "HEAD", "--not", *excluded),
            capture_output=True, text=True, timeout=30)
        for line in log.stdout.splitlines() if not log.returncode else ():
            sha, at = line.split()
            yield sha, float(at)

    def git(self, *args: str) -> str:
        run = subprocess.run(("git", "-C", str(self.checkout), *args),
                             capture_output=True, text=True, timeout=60)
        if run.returncode:
            raise ValueError(run.stderr.strip() or f"git {args[0]} failed")
        return run.stdout

    def readings(self) -> DistanceReadings:
        """The order's base, then each branch commit since the order; the last two are read."""
        base = self.git("merge-base", "HEAD", "origin/HEAD").strip()
        after = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.since))
        commits = [(sha, float(at)) for sha, at in (line.split() for line in reversed(self.git(
            "log", "--first-parent", f"--since={after}", "--format=%H %ct", "HEAD",
            "--not", base).splitlines()))]
        points = [(base, self.since), *commits][-2:]
        return DistanceReadings(
            tuple(DistanceReading(at, self.distance(base, sha)) for sha, at in points))

    def distance(self, base: str, sha: str) -> int:
        """The order's Check run on ``sha`` against its N, plus its numbered edits the diff since
        ``base`` has not touched."""
        check = self.todo.order.check
        touched = self.git("diff", "--name-only", base, sha).split()
        run = check.at(sha, str(self.checkout))
        return check.result.distance(run) + self.todo.order.untouched(touched)


@dataclass(frozen=True)
class OrderProgress(DeclaredFamily, affix="Progress"):
    """Where an order's work stands, derived on each read from its unit's checkout and PRs.

    Never stored: the checkout and GitHub are its owners."""

    @classmethod
    def read(cls, todo: Todo, storage: StorageRule, pulls: PullRequests) -> OrderProgress:
        """The first found member by its declared precedence; with none found, no commit yet."""
        owner = todo.holder
        assert owner, "Only a held order has progress."
        source = ProgressSource(todo, storage.order_checkout(owner, todo.id), pulls)
        members = sorted(cls.members_with(FoundProgress), key=lambda member: member.precedence)
        try:
            return next((progress for member in members for progress in member.found(source)),
                        NoCommitProgress())
        except (ValueError, subprocess.TimeoutExpired) as unread:
            return UnreadProgress(str(unread))

    @property
    @abstractmethod
    def state(self) -> str: ...

    @property
    def line(self) -> str:
        return self.state + self.distance_readings().trend.projection

    def distance_readings(self) -> DistanceReadings:
        """The order's distance readings; progress that read no commit has none."""
        return DistanceReadings()

    def stalled(self, ordered: float, now: float) -> bool:
        return False

    def stalled_between(self, ordered: float, since: float, now: float) -> bool:
        """Whether this work entered its stall after ``since`` and before ``now``."""
        return False

    def untouched(self, now: float) -> bool:
        """An open pull request nobody has touched for five minutes."""
        return False

    def waiting_on_install(self, todo: Todo) -> tuple[Todo, ...]:
        """The held order this progress leaves waiting on the fleet's install."""
        return ()

    def missing_merges(self, source: ProgressSource) -> tuple[MergedProgress, ...]:
        """Linked merges this progress incorrectly reports as no commit."""
        return ()


@dataclass(frozen=True)
class FoundProgress(OrderProgress):
    """A state read finds in the checkout or on GitHub; the lowest ``precedence`` found wins."""
    precedence: ClassVar[int]

    @classmethod
    @abstractmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        """Each member of this class the source holds."""


class Stalling:
    """Work that stalls at a time its own class declares."""

    @abstractmethod
    def stall_at(self, ordered: float) -> float: ...

    def stalled(self, ordered: float, now: float) -> bool:
        return now > self.stall_at(ordered)

    def stalled_between(self, ordered: float, since: float, now: float) -> bool:
        return since < self.stall_at(ordered) < now


@dataclass(frozen=True)
class Moving(Stalling):
    """Work last moved ``at``; it stalls 15 minutes later with no commit, push or PR change."""
    at: float = field(kw_only=True)
    readings: DistanceReadings = field(default=DistanceReadings(), kw_only=True, compare=False)

    def stall_at(self, ordered: float) -> float:
        return self.at + 900

    def distance_readings(self) -> DistanceReadings:
        return self.readings


@dataclass(frozen=True)
class NoCommitProgress(Stalling, OrderProgress):
    def missing_merges(self, source: ProgressSource) -> tuple[MergedProgress, ...]:
        return tuple(MergedProgress.found(source))

    @property
    def state(self) -> str:
        return "no commit since the order"

    def stall_at(self, ordered: float) -> float:
        """No first commit within 10 minutes of the order."""
        return ordered + 600


@dataclass(frozen=True)
class UnreadProgress(OrderProgress):
    reason: str

    @property
    def state(self) -> str:
        return f"progress unread: {self.reason}"


@dataclass(frozen=True)
class CommittedProgress(Moving, FoundProgress):
    precedence: ClassVar[int] = 3
    sha: str

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(sha[:9], at=at, readings=source.readings())
                for sha, at in source.commits("--remotes"))

    @property
    def state(self) -> str:
        return f"committed {self.sha} at {_clock(self.at)}, not pushed"


@dataclass(frozen=True)
class PushedProgress(CommittedProgress):
    """Pushed past the default branch, with no PR naming the order yet."""
    precedence: ClassVar[int] = 4

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(sha[:9], at=at, readings=source.readings())
                for sha, at in source.commits("origin/HEAD"))

    @property
    def state(self) -> str:
        return f"pushed {self.sha} at {_clock(self.at)}, no PR"


@dataclass(frozen=True)
class OpenPrProgress(Moving, FoundProgress):
    precedence: ClassVar[int] = 2
    number: int
    head: str

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(pr.number, pr.head[:9], at=pr.updated_at, readings=source.readings())
                for pr in source.requests("open"))

    @property
    def state(self) -> str:
        return f"PR #{self.number} open at {self.head}"

    def untouched(self, now: float) -> bool:
        return now - self.at > 300


@dataclass(frozen=True)
class MergedProgress(FoundProgress):
    """A merge awaits its order record; its alarm does not itself draw a strike."""
    precedence: ClassVar[int] = 1
    sha: str

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(pr.merge[:9]) for pr in source.requests("merged")
                if pr.merge and (pr.merged_at or 0) >= source.since)

    @property
    def state(self) -> str:
        return f"merged as {self.sha}, awaiting record"

    def waiting_on_install(self, todo: Todo) -> tuple[Todo, ...]:
        return (todo,)


class InstallDeadline(DeclaredFamily, affix="InstallDeadline"):
    """The fleet's next install: one time for the bus and the unit that runs it, set by the
    commander when it plans an upgrade. Merged and delivered orders wait on it; none of them
    carries it as its own deadline. Install, maintenance and carry work is the installer's."""

    @staticmethod
    def path_in(root: Path) -> Path:
        return root / "install-deadline.json"

    @classmethod
    def read(cls, root: Path) -> InstallDeadline:
        path = cls.path_in(root)
        if not path.exists():
            return UnplannedInstallDeadline()
        record = json.loads(path.read_text())
        return PlannedInstallDeadline(RecordedEventTime(record.pop("at")), **record)

    @abstractmethod
    def alarm(self, waiting: list[str], now: float) -> list[str]:
        """One INSTALL line for every order waiting on install; it strikes nobody."""

    @abstractmethod
    def refusal(self, work: str, commander: str) -> str:
        """Why the commander does not run the installer's work itself."""


class UnplannedInstallDeadline(InstallDeadline):
    def alarm(self, waiting: list[str], now: float) -> list[str]:
        return [f"INSTALL not planned; waiting: {', '.join(waiting)}"] * bool(waiting)

    def refusal(self, work: str, commander: str) -> str:
        return (f"{work.capitalize()} is the installer's work, and no install is planned. "
                "Plan one with agent-comms-commander order install HH:MM UNIT, "
                f"then order the {work} from that unit.")


@dataclass(frozen=True)
class PlannedInstallDeadline(InstallDeadline):
    at: RecordedEventTime
    installer: str

    def plan(self, root: Path) -> None:
        path = self.path_in(root)
        staged = path.with_suffix(".tmp")
        staged.write_text(json.dumps({"at": self.at.at, "installer": self.installer}))
        staged.replace(path)

    def refusal(self, work: str, commander: str) -> str:
        return (f"{work.capitalize()} is {self.installer}'s work, as the planned installer, not "
                f"{commander}'s. Send it as an order: agent-comms-commander order send --to "
                f"{self.installer}, naming the {work} to run.")

    def alarm(self, waiting: list[str], now: float) -> list[str]:
        if not waiting:
            return []
        due = time.strftime("%H:%M", time.localtime(self.at.at))
        if now > self.at.at:
            return [f"INSTALL LATE (due {due}), not the orders; waiting: {', '.join(waiting)}"]
        return [f"INSTALL due {due}; waiting: {', '.join(waiting)}"]


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


@dataclass(frozen=True)
class OrderWords(WireValue):
    """Text the commander writes; quoted, backticked, indented and path-like spans are someone else's words."""

    CITED: ClassVar[re.Pattern] = re.compile(
        r'(?m)^ {4}.*$|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|\S*/\S*|\S+\.(?:py|md|jsonl?|txt|png|toml|ts|log|patch)\b')
    REFUSED: ClassVar[re.Pattern]
    refusal: ClassVar[str]
    least_words: ClassVar[int] = 1
    text: str

    def __post_init__(self) -> None:
        if refused := self.refused(self.text):
            raise ValueError(refused)

    def to_wire(self) -> str:
        return self.text

    @classmethod
    def from_wire(cls, data: object) -> Self:
        if not isinstance(data, str):
            raise MalformedRecord(f"Expected {cls.__name__}, received {type(data).__name__}")
        with MalformedRecord.refusing(f"Invalid {cls.__name__}"):
            return cls(data)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(str)

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
    """What a unit did, plainly, in enough words to stand on the public record. A unit's
    wording is answered by a correction, never a strike: a reason whose every quoted span
    is jargon is about wording alone."""

    refusal = "Say what the unit did plainly in {}+ words; jargon {} hides what happened."
    least_words = 10

    @classmethod
    def refused(cls, text: str) -> str | None:
        cited = [span.group(0).strip('`"“” ') for span in cls.CITED.finditer(text)]
        if cited and all(PlainText.found(span) for span in cited):
            return f"{cited} is the unit's wording; send it a correction, not a strike."
        return super().refused(text)


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


class DefectText(OrderWords):
    """A text naming a defect in a Slop: line also says, outside that line, what was wrong;
    a text naming no defect needs no label."""

    SLOP: ClassVar[re.Pattern] = re.compile(r'(?m)^Slop: .*$')
    REFUSED, refusal = BluntText.REFUSED, BluntText.refusal  # the label is BluntText's own

    @classmethod
    def found(cls, text: str) -> list[str]:
        return [] if BluntText.found(cls.SLOP.sub(' ', text)) else cls.SLOP.findall(text)


class DirectiveText:
    """What the commander and the owner's voice send units: order send, order say and the
    connector's send refuse a text failing any of these."""

    OUTGOING: ClassVar[tuple[type[OrderWords], ...]] = (
        FairText, HierarchyText, PlainText, OrderText, DefectText)

    @classmethod
    def refused(cls, text: str) -> tuple[str, ...]:
        return tuple(refused for family in cls.OUTGOING if (refused := family.refused(text)))


class OrderRun(DeclaredFamily, affix="OrderRun"):
    label: ClassVar[str] = "Works when:"

    @abstractmethod
    def instructions(self) -> tuple[str, ...]: ...

    @abstractmethod
    def require_closing(self, works: str) -> None:
        """Refuse closing evidence that does not show this run happened."""


@dataclass(frozen=True)
class MechanicalOrderRun(OrderRun):
    def instructions(self) -> tuple[str, ...]:
        return ()

    def require_closing(self, works: str) -> None:
        """The order's check alone closes it."""


@dataclass(frozen=True)
class ApplicationOrderRun(OrderRun):
    text: str
    least_words: ClassVar[int] = 8  # the run, and the evidence that it happened
    quoted_words: ClassVar[int] = 4

    def __post_init__(self) -> None:
        if len(self.text.split()) < self.least_words:
            raise ValueError(f'Add "{self.label} <the user-visible closing run, '
                             f'{self.least_words}+ words>"; a grep never closes a problem.')

    def instructions(self) -> tuple[str, ...]:
        return (f"{self.label} {self.text}",)

    def require_closing(self, works: str) -> None:
        quoted = self.phrases(self.text) & self.phrases(works)
        if len(works.split()) < self.least_words or not quoted:
            raise ValueError(f"Describe what worked in {self.least_words}+ words, quoting "
                             f"{self.quoted_words}+ consecutive words of: {self.label} {self.text}")

    @classmethod
    def phrases(cls, text: str) -> set[tuple[str, ...]]:
        words = [word.strip(".,;:!?\"'`()").lower() for word in text.split()]
        return {tuple(words[at:at + cls.quoted_words])
                for at in range(len(words) - cls.quoted_words + 1)}


class OrderDeletion(DeclaredFamily, affix="Deletion"):
    """The decision family an order deletes, read once from its Deletes: line."""

    label: ClassVar[str] = "Deletes:"
    LINE: ClassVar[re.Pattern] = re.compile(r"(.+?)\s+at\s+(\S+):(\d+)\s*::\s*(\S+)\s*::\s*(.+)")
    checks: ClassVar[tuple[OrderCheck, ...]] = ()

    @staticmethod
    def parse(line: str | None) -> OrderDeletion:
        if line is None:
            return NoOrderDeletion()
        if not (found := OrderDeletion.LINE.fullmatch(line.strip())):
            raise ValueError(f"Write {OrderDeletion.label} <decision family> at PATH:LINE :: "
                             "<repo> :: <family-wide scanner count>.")
        return FamilyDeletion(found[1], FileLine(found[2], int(found[3])),
                              OrderCheck(found[4], found[5], CountCheckResult(0)))

    def instructions(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class NoOrderDeletion(OrderDeletion):
    pass


@dataclass(frozen=True)
class FamilyDeletion(OrderDeletion):
    """Completion counts the whole family on the delivered commit, so a site renamed or wrapped
    instead of deleted still counts, and only zero passes."""

    family: str
    site: FileLine
    count: OrderCheck

    @property
    def checks(self) -> tuple[OrderCheck, ...]:
        return (self.count,)

    def instructions(self) -> tuple[str, ...]:
        return (f"{self.label} {self.family} at {self.site.to_wire()} :: {self.count.directory} :: "
                f"{self.count.command}",)


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
    run: OrderRun
    deletes: OrderDeletion = field(
        default_factory=NoOrderDeletion, metadata={"wire_omit_default": True})
    serves: type[OrderPhase] = field(default=UnplannedPhase, metadata={"wire_omit_default": True})

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
    line: ClassVar[str]
    NAMED_FILE: ClassVar[re.Pattern] = re.compile(r"([\w./-]+\.\w+):\d+")

    def require_transfer(self, todo: Todo) -> None:
        raise ValueError("Only an undelivered order can change owner.")

    def reissued(self, at: float) -> PendingProblemOrder:
        """This order sent again at ``at`` to a new owner, its first-call window kept."""
        raise ValueError("Only an undelivered order can change owner.")

    def untouched(self, touched: Sequence[str]) -> int:
        """The numbered edits naming a file:line where ``touched`` changed none of those files."""
        def changed(name: str) -> bool:
            return any(path.endswith(name.lstrip("/")) or name.endswith("/" + path)
                       for path in touched)
        return sum(1 for edit in self.edits
                   if (named := self.NAMED_FILE.findall(edit)) and not any(map(changed, named)))

    def copy_as[Kind: ProblemOrder](self, kind: type[Kind], **evidence) -> Kind:
        return kind(**{item.name: getattr(self, item.name) for item in fields(self)}, **evidence)

    pull_request_label: ClassVar[str] = "Agent-Comms-Order:"

    @classmethod
    def pull_request_ids(cls, body: str) -> tuple[str, ...]:
        return tuple(line.removeprefix(cls.pull_request_label).strip()
                     for line in body.splitlines() if line.startswith(cls.pull_request_label))

    @property
    def pull_request_reference(self) -> str:
        return f"{self.pull_request_label} {self.id}"

    def pull_request_body(self, body: str) -> str:
        declared = self.pull_request_ids(body)
        if any(order != self.id for order in declared):
            raise ValueError("The pull request body names a different order.")
        content = "\n".join(line for line in body.splitlines()
                            if not line.startswith(self.pull_request_label))
        return f"{content.rstrip()}\n\n{self.pull_request_reference}\n"

    def open_pull_request(self, todo: Todo, checkout: Path, title: str, body: str) -> str:
        """The order creates its PR with the immutable identity every progress reader uses."""
        created = subprocess.run(
            ("gh", "pr", "create", "-R", todo.repo, "--title", title, "--body-file", "-"),
            input=self.pull_request_body(body), cwd=checkout,
            capture_output=True, text=True, timeout=60)
        if created.returncode:
            raise ValueError(created.stderr.strip() or "Order pull request creation failed.")
        return created.stdout.strip()

    def pull_request_instruction(self, problem: str) -> str:
        return (f"Open the PR with agent-comms-commander order pr {problem} "
                "--title '<title>' --body-file <file>; Core writes its required order identity.")

    def instructions(self, todo):
        return "\n".join((f"Problem: {todo.id}: {todo.text}", f"Order: {self.id}",
            f"Owner: {self.owner.to_wire()}", self.pull_request_instruction(todo.id),
            f"Check: {self.check.directory} :: {self.check.command}",
            *self.deletes.instructions(),
            *self.run.instructions(),
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

    def installed(self, works: str, artifact: str) -> ProblemOrder:
        raise ValueError("Check the delivered order before recording installation.")

    def require_checked(self, problem: str) -> None:
        """An installed claim stands on this order's recorded check result."""
        raise ValueError(f"An installed claim needs the recorded check result of {problem}; "
                         "its order has none.")

    def require_close(self):
        raise ValueError("Closing requires recorded installed application evidence.")

    def withdrawn(self, previous: Assignment, why: str, superseded_by: str | None) -> ProblemOrder:
        raise ValueError("Only an undelivered order can be withdrawn.")

    def withdrawals(self, todo: Todo) -> tuple[str, ...]:
        """How this order retired its problem without delivery, as the record and board say."""
        return ()

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        """The order waiting on the fleet's install to record it working."""
        return ()


@dataclass(frozen=True, kw_only=True)
class PendingProblemOrder(ProblemOrder):
    def reissued(self, at: float) -> PendingProblemOrder:
        window = (self.first_call_deadline.recorded_at("A first-call deadline")
                  - self.at.recorded_at("An order"))
        return replace(self, id=uuid4().hex[:12], at=RecordedEventTime(at),
                       first_call_deadline=RecordedEventTime(at + window),
                       first_call=AwaitingFirstCall())

    pending: ClassVar[bool] = True
    line: ClassVar[str] = "Open"

    def require_transfer(self, todo: Todo) -> None:
        pass

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        """An undelivered order waits on install once its PR merged."""
        return read[todo.id].waiting_on_install(todo)

    def withdrawn(self, previous: Assignment, why: str, superseded_by: str | None) -> ProblemOrder:
        return self.copy_as(WithdrawnProblemOrder, previous=previous, why=why,
                            superseded_by=superseded_by)


@dataclass(frozen=True, kw_only=True)
class DeliveredProblemOrder(ProblemOrder):
    delivery: OrderDelivery
    pending: ClassVar[bool] = False
    line: ClassVar[str] = "Delivered, waiting on install"

    def require_transfer(self, todo: Todo) -> None:
        owner = (todo.assignment or todo.released[-1].previous).owner
        raise ValueError(f"{owner} already has {self.delivery.work}; {owner} merges it.")

    def delivered(self, delivery):
        if delivery != self.delivery:
            raise ValueError("The order already records a different delivery.")
        return self

    def corrected(self, correction: CorrectCheckTodo) -> ProblemOrder:
        raise ValueError("A delivered order keeps the check its delivery passed.")

    def installed(self, works, artifact):
        self.run.require_closing(works)
        return self.copy_as(InstalledProblemOrder, works=works, artifact=artifact)

    def require_checked(self, problem: str) -> None:
        """The delivery passed the order's check, so its result is recorded."""

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return (todo,)


@dataclass(frozen=True, kw_only=True)
class InstalledProblemOrder(DeliveredProblemOrder):
    works: str
    artifact: str
    line: ClassVar[str] = "Installed"

    def __post_init__(self):
        super().__post_init__()
        if not self.works.strip() or not self.artifact.strip():
            raise ValueError("Describe what worked and name its artifact file.")

    def installed(self, works, artifact):
        if (works, artifact) != (self.works, self.artifact):
            raise ValueError("The order already records different installed evidence.")
        return self

    def require_close(self):
        pass

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class WithdrawnProblemOrder(ProblemOrder):
    """An order the commander sent in error and retired before delivery; it strikes no one."""

    previous: Assignment
    why: str
    superseded_by: str | None = field(default=None, metadata={"wire_omit_default": True})
    pending: ClassVar[bool] = False
    line: ClassVar[str] = "Withdrawn"

    def withdrawals(self, todo: Todo) -> tuple[str, ...]:
        superseded = f" (superseded by {self.superseded_by})" if self.superseded_by else ""
        return (f"{todo.id}: order {self.id} withdrawn from {self.previous.owner}, "
                f"no strike: {self.why}{superseded}",)


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


class GoalReference(SubclassUnion):
    @abstractmethod
    def request(self) -> str:
        """The commander's request goal this problem serves."""

    def require_reference(self) -> None:
        pass

    def threads(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class NoGoalReference(NullMember, GoalReference):
    def request(self) -> str:
        raise TodoError("A problem with no request goal cannot be ordered again.")


@dataclass(frozen=True)
class GoalRef(RecordMember, GoalReference):

    owner: str
    owner_created: float
    goal_id: str

    def __post_init__(self) -> None:
        self.require_reference()

    def require_reference(self) -> None:
        _text(self.owner, "Thread name", 128)
        try:
            self.incarnation.require_recorded()
        except ValueError as error:
            raise TodoError("Goal reference requires its recorded owner incarnation") from error
        _text(self.goal_id, "Goal ID", 128)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created)

    def threads(self) -> tuple[str, ...]:
        return (self.owner,)

    def request(self) -> str:
        return self.goal_id


@dataclass(frozen=True, slots=True)
class Assignment:
    owner: str
    owner_created: float
    parent: str
    parent_created: float
    generation: str
    previous_owners: tuple[ThreadIncarnation, ...] = field(
        default=(), metadata={"wire_omit_default": True})

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

    def require_trace(self, owner: str, traced: FileLine | None) -> None:
        owners = {self.owner, *(previous.name for previous in self.previous_owners)}
        if owner != self.owner and len(owners) >= 2 and traced is None:
            raise ValueError(f"This problem has had {sorted(owners)}; read the failing path "
                             "and pass --traced PATH:LINE.")
        if traced is not None:
            traced.require_exists()

    def successor(self, proposed: Assignment, current: Todo) -> Assignment:
        if self.generation == proposed.generation:
            raise TodoConflict(current)
        return self.handed_to(proposed)

    def handed_to(self, proposed: Assignment) -> Assignment:
        previous = self.previous_owners
        if self.owner_identity != proposed.owner_identity:
            previous = (*previous, self.owner_identity)
        return replace(proposed, previous_owners=previous)


class TodoState(DeclaredFamily, affix="TodoState"):
    open: ClassVar[bool] = True  # on the board until closed with installed evidence

    @classmethod
    def open_names(cls) -> tuple[str, ...]:
        return tuple(member.declared_name for member in cls.members_with(cls) if member.open)

    @classmethod
    def require_open(cls, current: Todo) -> None:
        pass

    @classmethod
    def prepare_change(cls, goals, current):
        pass

    @classmethod
    def assign(cls, current: Todo, proposed: Assignment, traced: FileLine | None = None) -> Todo:
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
    def assign(cls, current, proposed, traced=None):
        return current.assigned(proposed, traced)


class BlockedTodoState(TodoState):
    pass


class DoneTodoState(TodoState):
    open: ClassVar[bool] = False

    @classmethod
    def require_open(cls, current):
        raise TodoError(f"{current.id} is done; a strike or hunt is recorded on an open problem.")

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

    def record(self, goals: Goals, problem: str) -> Todo:
        """Write this operation; one that also revises the holder's Goal routes through it."""
        return goals.apply_problem(problem, self)

    @abstractmethod
    def apply(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    proposed: Assignment
    deadline: EventTime
    order: PendingProblemOrder
    goal: GoalRef
    traced: FileLine | None = field(default=None, metadata={"wire_omit_default": True})

    def apply(self, current: Todo) -> Todo:
        self.deadline.require_deadline()
        proposed = current.proposed_assignment(self.proposed)
        if (current.assignment, current.deadline, current.order, current.goal) == (
                proposed, self.deadline, self.order, self.goal):
            return current
        current.require_revision(self.expected_revision)
        current.require_commander(proposed.parent_identity)
        assigned = (current if current.assignment == proposed
                    else current.state.assign(current, proposed, self.traced))
        return replace(assigned, deadline=self.deadline, order=self.order, goal=self.goal,
                       revision=current.revision + 1)


@dataclass(frozen=True)
class StrikeTodo(TodoOperation):
    actor: ThreadIncarnation
    previous: Assignment
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.require_assignment(self.previous)
        _text(self.reason, "Strike reason", 2000)
        StrikeReason(self.reason)
        if any(self.repeats(recorded) for recorded in current.live_strikes(self.previous.owner)):
            return current
        return replace(current, strikes=(*current.strikes, self), revision=current.revision + 1)

    def repeats(self, recorded: StrikeTodo) -> bool:
        return False


@dataclass(frozen=True)
class StorageStrikeTodo(StrikeTodo):
    """Repeated reports of one storage violation retain its recorded strike."""

    def repeats(self, recorded: StrikeTodo) -> bool:
        return (self.actor == recorded.actor and self.previous.owner == recorded.previous.owner
                and self.reason == recorded.reason)


@dataclass(frozen=True)
class VerdictAudience:
    """The threads a send is for; a send reaching many threads wakes no thread outside it."""
    threads: tuple[str, ...]

    @classmethod
    def of(cls, unit: str, goal: GoalReference) -> VerdictAudience:
        """An order's unit and its goal's thread; no verdict kind today concerns every unit."""
        return cls(tuple(dict.fromkeys((unit, *goal.threads()))))

    def addressed(self, target: str, body: str) -> str:
        return CommandTarget.named(target).addressed(self.threads, body)


@dataclass(frozen=True)
class OrderVerdict(TodoOperation, declared_name=None):
    """The commander's verdict on one tool call the holder made for its order, kept on the order."""
    audience: ClassVar[type[VerdictAudience]] = VerdictAudience
    actor: ThreadIncarnation
    previous: Assignment
    tool_call_id: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()), kw_only=True)

    @abstractmethod
    def judged(self, current: Todo) -> Todo: ...

    @property
    @abstractmethod
    def line(self) -> str: ...

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.require_assignment(self.previous)
        _text(self.tool_call_id, "Judged tool call ID", 200)
        judged = self.judged(current)
        return replace(judged, verdicts=(*judged.verdicts, self), revision=current.revision + 1)


@dataclass(frozen=True)
class AcceptedVerdict(OrderVerdict):
    """The tool call carries out the order clause it quotes."""
    quote: str
    line = "Accepted"

    def judged(self, current: Todo) -> Todo:
        quoted = " ".join(self.quote.split())
        order = " ".join(current.order.instructions(current).split())
        if len(quoted.split()) < 4 or quoted not in order:
            raise ValueError("Quote 4+ words of the order this tool call carries out.")
        return current


@dataclass(frozen=True)
class SendBackVerdict(OrderVerdict):
    """The tool call goes back; the same write strikes the holder and revises the order Goal."""
    reason: StrikeReason

    @property
    def line(self) -> str:
        return f"Sent back: {self.reason.text}"

    def judged(self, current: Todo) -> Todo:
        strike = StrikeTodo(self.expected_revision, self.actor, self.previous, self.reason.text,
                            self.at)
        return strike.apply(current)

    def record(self, goals: Goals, problem: str) -> Todo:
        return goals.send_back(problem, self)


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
class ReleaseTodo(TodoOperation):
    """The commander takes an open problem off its unit; the order and its strikes stay."""
    actor: ThreadIncarnation
    previous: Assignment
    reason: StrikeReason
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.require_assignment(self.previous)
        return replace(current, assignment=None, released=(*current.released, self),
                       revision=current.revision + 1)


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
class WordingCorrection(TodoOperation):
    """Core's answer to a published unit message whose words hide what happened; no strike."""
    message_id: str
    words: tuple[str, ...]

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        corrections = (*current.corrections, self)
        return replace(current, corrections=corrections, revision=current.revision + 1)


@dataclass(frozen=True)
class OrderMessage(TodoOperation):
    """The bus message the commander sent this order in; a verdict citing it lands here."""
    actor: ThreadIncarnation
    message_id: str

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        _text(self.message_id, "Order message ID", 200)
        return replace(current, messages=(*current.messages, self), revision=current.revision + 1)


@dataclass(frozen=True)
class ServeTodo(TodoOperation):
    """The commander names the plan phase an open order serves; it keeps its id and holder."""
    actor: ThreadIncarnation
    serves: type[OrderPhase]

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(current, order=replace(current.order, serves=self.serves),
                       revision=current.revision + 1)


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
        verified = self.delivery.verified(current.order.check, current.order.deletes)
        return replace(self, delivery=asyncio.run(verified))

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(current, order=current.order.delivered(self.delivery), revision=current.revision + 1)


@dataclass(frozen=True)
class CloseOrderTodo(TodoOperation, declared_name=None):
    """The commander records the order's terminal state and closes its problem in one revision."""
    actor: ThreadIncarnation

    @abstractmethod
    def closed(self, current: Todo) -> ProblemOrder: ...

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        closed = replace(current, order=self.closed(current))
        return DoneTodoState.decide(closed, ChangeTodoState(
            current.revision, DoneTodoState, self.actor, current.assignment.generation))


@dataclass(frozen=True)
class InstallTodo(CloseOrderTodo):
    works: str
    artifact: str

    def closed(self, current: Todo) -> ProblemOrder:
        if not Path(self.artifact).is_file():
            raise ValueError("Retain the installed application artifact before closing.")
        return current.order.installed(self.works, self.artifact)


@dataclass(frozen=True)
class WithdrawTodo(CloseOrderTodo):
    """An undelivered order sent in error leaves its problem with its reason and no strike."""
    previous: Assignment
    why: str
    superseded_by: str | None = None

    def closed(self, current: Todo) -> ProblemOrder:
        current.require_assignment(self.previous)
        _text(self.why, "Withdrawal reason", 2000)
        return current.order.withdrawn(self.previous, self.why, self.superseded_by)


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
    indexes = (Index(("creator", "creator_created", "state")), Index(("state",)))

    id: str = field(metadata={"sql": Column(primary_key=True)})
    repo: str
    text: str
    creator: str
    creator_created: float
    state: type[TodoState]
    revision: int = field(metadata={"sql": Column(check="revision>0")})
    goal: GoalReference = field(metadata={"sql": Column(storage=JsonStorage, nullable=True)})
    assignment: Assignment | None
    order: ProblemOrder
    deadline: EventTime = field(default_factory=UnrecordedEventTime)
    strikes: tuple[StrikeTodo, ...] = ()
    hunts: tuple[Hunt, ...] = ()
    voided: tuple[VoidStrikeTodo, ...] = ()
    released: tuple[ReleaseTodo, ...] = ()
    verdicts: tuple[OrderVerdict, ...] = ()
    corrections: tuple[WordingCorrection, ...] = ()
    messages: tuple[OrderMessage, ...] = ()

    @property
    def pending(self):
        return self.order.pending

    def overdue(self, now: float) -> bool:
        """An undelivered order remains past due until its result is recorded."""
        return self.pending and self.deadline.recorded_at("An order deadline") < now

    @property
    def creator_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.creator, self.creator_created)

    @property
    def holder(self) -> str | None:
        """The unit holding this problem; None once it is closed or released."""
        return None if self.assignment is None else self.assignment.owner

    def held(self) -> Assignment:
        """The open order's assignment; a problem nobody holds has no order work."""
        if self.assignment is None:
            raise TodoError(f"{self.id} has no open order.")
        return self.assignment

    @property
    def strike_count(self) -> int:
        return 0 if self.assignment is None else len(self.live_strikes(self.assignment.owner))

    def live_strikes(self, unit: str | None = None) -> tuple[StrikeTodo, ...]:
        """The strikes still counting on this task, against ``unit`` on any order of it."""
        taken_back = tuple(void.strike for void in self.voided)
        return tuple(recorded for recorded in self.strikes
                     if unit in (None, recorded.previous.owner) and recorded not in taken_back)

    def struck_on_order(self, unit: str) -> bool:
        """A live strike on ``unit`` recorded since the current order was sent; a send-back that
        replaces this order is that same miss, so it counts no second strike."""
        sent = self.order.at.recorded_at("An order")
        return any(recorded.at.recorded_at("A strike") >= sent
                   for recorded in self.live_strikes(unit))

    def struck_out(self, unit: str) -> bool:
        """The unit has taken its problem goal's StrikeLimit live strikes here; only mechanical
        orders reach it."""
        return len(self.live_strikes(unit)) >= ProblemGoalOrder.StrikeLimit

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

    def proposed_assignment(self, proposed: Assignment) -> Assignment:
        if self.assignment is not None:
            return replace(proposed, previous_owners=self.assignment.previous_owners)
        if self.released:  # the released owner joins the next owner's history
            return self.released[-1].previous.handed_to(proposed)
        return proposed

    def assigned(self, proposed: Assignment, traced: FileLine | None = None) -> Todo:
        self.order.require_transfer(self)
        if self.assignment is not None:
            self.assignment.require_trace(proposed.owner, traced)
            proposed = self.assignment.successor(proposed, self)
        return replace(self, assignment=proposed, revision=self.revision + 1)

    def require_released_order(self, owner: ThreadIncarnation, goal_id: str) -> None:
        if self.assignment is not None and self.assignment.owned_by(owner, goal_id):
            raise TodoConflict(self)

    def sent_in(self, message_id: str) -> bool:
        """Whether the commander sent this order in that bus message."""
        return any(message.message_id == message_id for message in self.messages)

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

    def board_disagreements(
        self, commander: ThreadIncarnation, storage: StorageRule,
    ) -> Iterator[tuple[Todo, MergedProgress]]:
        """Open orders whose progress misses a linked merge, sharing one PR read cache."""
        todos = self.held_for_commander(commander)
        pulls = PullRequests(todos)
        for todo in todos:
            progress = OrderProgress.read(todo, storage, pulls)
            source = ProgressSource(todo, storage.order_checkout(todo.held().owner, todo.id), pulls)
            for merged in progress.missing_merges(source):
                yield todo, merged

    def holds_pending(self, owner: ThreadIncarnation) -> bool:
        """Whether a problem assigned to owner pends, under any generation; its Goal stays open."""
        return any(todo.pending for todo in self._open(
            "json_extract(assignment,'$.owner')=? AND json_extract(assignment,'$.owner_created')=?",
            (owner.name, owner.wire_birth)))

    def for_request(self, commander: ThreadIncarnation, goal_id: str) -> tuple[Todo, ...]:
        """Every order that served one request, a Goal on the commander's thread, open or done."""
        return self._select(
            "json_extract(goal,'$.owner')=? AND json_extract(goal,'$.owner_created')=? "
            "AND json_extract(goal,'$.goal_id')=?", (commander.name, commander.wire_birth, goal_id))

    def for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._open("creator=? AND creator_created=?", (commander.name, commander.wire_birth))

    def held_for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._open("creator=? AND creator_created=? AND assignment IS NOT NULL",
            (commander.name, commander.wire_birth))

    def held_by(self, commander: ThreadIncarnation, unit: str) -> tuple[Todo, ...]:
        return self._open(
            "creator=? AND creator_created=? AND json_extract(assignment,'$.owner')=?",
            (commander.name, commander.wire_birth, unit))

    def history_for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._select("creator=? AND creator_created=?", (commander.name, commander.wire_birth))

    def units(self, commander: ThreadIncarnation) -> frozenset[str]:
        """The threads this commander gives orders to: everyone who holds or held one of its
        problems."""
        return frozenset(
            assignment.owner for todo in self.history_for_commander(commander)
            for assignment in (todo.assignment, *(release.previous for release in todo.released))
            if assignment)

    def _open(self, where: str, parameters: tuple) -> tuple[Todo, ...]:
        states = TodoState.open_names()
        selected = ','.join('?' for state in states)
        return self._select(f"state IN ({selected}) AND ({where})", (*states, *parameters))

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
        goal: GoalRef,
    ) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 2000)
        repo_id = _repo(repo)
        identity, assignee = _participant(creator), _thread(owner)
        deadline.require_deadline()
        goal.require_reference()
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

    def correct(self, unit: str, message_id: str, text: str) -> tuple[Todo, ...]:
        """Correct every open order the unit holds when its published text uses jargon."""
        if not (words := tuple(PlainText.found(text))) or not self.path.exists():
            return ()
        with self.writing() as db:
            held = (todo for todo in self._rows(db, "json_extract(assignment,'$.owner')=?", (unit,))
                    if todo.state.open)
            corrected = tuple(WordingCorrection(todo.revision, message_id, words).apply(todo)
                              for todo in held)
            for todo in corrected:
                todo.upsert(db)
            return corrected

    def fire(self, unit: str, actor: ThreadIncarnation, reason: StrikeReason) -> tuple[Todo, ...]:
        """Release every open problem the unit holds from this commander in one write; each
        records the same reason, and its order and strikes stay."""
        with self.writing() as db:
            held = tuple((todo, assignment) for todo in self._rows(
                db, "json_extract(assignment,'$.owner')=? AND creator=? AND creator_created=?",
                (unit, actor.name, actor.wire_birth))
                if todo.state.open and (assignment := todo.assignment))
            if not held:
                raise TodoError(f"{unit} holds no open problem.")
            released = tuple(ReleaseTodo(todo.revision, actor, assignment, reason).apply(todo)
                             for todo, assignment in held)
            for todo in released:
                todo.upsert(db)
            return released

    def assign(
        self,
        todo_id: str,
        *,
        expected_revision: int,
        owner: Thread,
        parent: Thread,
        generation: str,
        goal: GoalRef,
        deadline: EventTime,
        order: PendingProblemOrder,
        traced: FileLine | None = None,
    ) -> Todo:
        identity, parent_identity = _thread(owner), _thread(parent)
        proposed = Assignment(
            identity.name,
            identity.created_at,
            parent_identity.name,
            parent_identity.created_at,
            generation,
        )
        return self.apply(todo_id, AssignTodo(expected_revision, proposed, deadline, order, goal, traced))
