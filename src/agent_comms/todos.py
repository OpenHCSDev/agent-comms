"""A durable todo owns its one assignment; it does not start an agent.

This is an opt-in post-PR1 primitive. An ACP/fork adapter must verify current
thread identities and reserve a todo BEFORE spawning a worker. File claims,
collaboration edges and thread goals remain separate authorities.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import re
import sqlite3
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from functools import cache, cached_property
from importlib.resources import files
from math import ceil
from pathlib import Path
from statistics import median
from types import get_original_bases
from typing import TYPE_CHECKING, Annotated, ClassVar, Self, get_args, get_origin
from uuid import uuid4

from agent_comms import skills
from agent_comms.build_identity import (
    BuildIdentity,
    CommitInstallation,
    RecordedBuildIdentity,
    UnrecordedBuildIdentity,
)
from agent_comms.bus_publication import AgentOrigin
from agent_comms.change_report import (
    FileLine,
    MainRevision,
    NoSourceReference,
    PullRequestHeadRevision,
    SourceReference,
    SourceRevision,
)
from agent_comms.channel_targets import CommandTarget
from agent_comms.child_process import (
    BoundedRun,
    CheckPrinted,
    ChildResult,
    ExitedOutcome,
    NoIntegerPrinted,
    OrderCheckFailedError,
)
from agent_comms.command import Command
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import (
    FieldCodec,
    GitCommitText,
    MalformedRecord,
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
    WireOmitDefault,
    WireTransient,
    WireValue,
    boundary_decoder,
)
from agent_comms.goals import CheckedCommit, OrderCheck, ProblemGoalOrder
from agent_comms.locked_store import RegistryFile
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.message_reference import MessageReference
from agent_comms.messages import InfoKind, Message
from agent_comms.order_runs import OrderRun
from agent_comms.order_story import ProblemStory, StoryEvent, StoryLine, StorySource
from agent_comms.plan_phases import OrderPhase, UnplannedPhase
from agent_comms.private_bus_checkpoint import PublicMessages
from agent_comms.progress_claims import MergedClaim, ProgressClaim
from agent_comms.recorded_time import (
    EventTime,
    RecordedEventTime,
    UnrecordedEventTime,
)
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_identity import (
    CoreWatcherIncarnation,
    RecordedThreadIncarnation,
    ThreadIncarnation,
)
from agent_comms.threads import Thread
from agent_comms.typed_table import (
    Column,
    DurableStore,
    ExpressionIndex,
    Index,
    JsonStorage,
    TextStorage,
    TypedTable,
)
from agent_comms.work_branches import WorkBranchError, WorkBranches

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goal_management import Goals
    from agent_comms.registration import Registration


@dataclass(frozen=True)
class DeliveryEvidence(DeclaredFamily, affix="DeliveryEvidence"):
    def artifact(self) -> str:
        raise ValueError("No application evidence was supplied at delivery; pass --artifact FILE.")


@dataclass(frozen=True)
class NoDeliveryEvidence(DeliveryEvidence):
    pass


@dataclass(frozen=True)
class FileDeliveryEvidence(DeliveryEvidence):
    path: str

    def artifact(self) -> str:
        if not Path(self.path).is_file():
            raise ValueError("Delivery evidence must name an existing file.")
        return self.path


@dataclass(frozen=True, kw_only=True)
class OrderDelivery(DeclaredFamily, StoryEvent, affix="Delivery"):
    at: EventTime = field(default_factory=UnrecordedEventTime)

    @property
    def installation_commits(self) -> tuple[str, ...]:
        return ()

    def merged_commits(self, repo: Repo) -> tuple[str, ...]:
        return ()

    def check_failed(self, goals: Goals, todo: Todo, error: OrderCheckFailedError) -> None:
        """The one false-completion writer, shared by every checked delivery."""
        now = time.time()
        spent = (now - todo.order.checked().at.recorded_at("An order")) / 60
        FailedCheckStrike.make(todo, now,
            f"reported done with {self.work} but its order check failed: {error}; "
            f"cost {spent:.1f} minutes").record(goals, todo.id)

    @abstractmethod
    async def verified(
        self, todo: Todo, registry: Registration,
        verified_claims: dict[ProgressClaim, tuple[str, ...]],
    ) -> OrderDelivery: ...

    def closing_artifact(self) -> str:
        raise ValueError("A triage report is not installed application evidence.")

    @property
    def installation_build(self) -> BuildIdentity:
        raise ValueError("A triage report cannot be installed.")

    @property
    def work(self) -> str:
        return "delivered work"

    @property
    def line(self) -> str:
        return self.work

    def story_text(self) -> str:
        return self.work


@dataclass(frozen=True, kw_only=True)
class CheckedDelivery(OrderDelivery, declared_name=None):
    """Implementation deliveries share source checks and installed-build evidence."""

    evidence: Annotated[DeliveryEvidence, WireOmitDefault()] = field(
        default_factory=NoDeliveryEvidence
    )
    proof_build: Annotated[BuildIdentity, WireOmitDefault()] = field(
        default_factory=UnrecordedBuildIdentity
    )

    def closing_artifact(self) -> str:
        return self.evidence.artifact()

    @property
    def installation_build(self) -> BuildIdentity:
        return self.proof_build

    async def run(self, check, *command):
        result = await BoundedRun.run(tuple(command), cwd=check.directory, timeout=120)
        if result.outcome != ExitedOutcome(0):
            raise ValueError(result.stderr.decode(errors="replace") or "Delivery source check failed.")
        return result.stdout.decode(errors="replace")

    def required_claims(self, claims: tuple[ProgressClaim, ...]) -> tuple[ProgressClaim, ...]:
        return claims

    async def verified(
        self, todo: Todo, registry: Registration,
        verified_claims: dict[ProgressClaim, tuple[str, ...]],
    ) -> CheckedDelivery:
        """The order's check and every count its deletion declares run on the delivered source."""
        order = todo.order.checked()
        check = order.check
        for each in (check, *order.deletes.checks):
            await self.require_source(each)
            each.result.require(await self.checked(each, registry.storage_rule.checkout_base))
        return replace(self, at=await self.delivery_time(check),
                       proof_build=await asyncio.to_thread(self.proof_build.recorded))

    @abstractmethod
    async def require_source(self, check): ...

    @abstractmethod
    async def checked(self, check: OrderCheck, checkout_base: Path) -> ChildResult: ...

    @abstractmethod
    async def delivery_time(self, check): ...


@dataclass(frozen=True, kw_only=True)
class RecordedCommitDelivery(CheckedDelivery, declared_name="commit"):
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

    @property
    def installation_commits(self) -> tuple[str, ...]:
        return (self.delivered,)

    def merged_commits(self, repo: Repo) -> tuple[str, ...]:
        return (self.delivered,) if repo.contains(self.delivered, "origin/main") else ()

    def check_failed(self, goals: Goals, todo: Todo, error: OrderCheckFailedError) -> None:
        if self.commit != self.delivered:
            return  # A failed integration check is not evidence that the unit's commit failed.
        super().check_failed(goals, todo, error)

    async def require_source(self, check: OrderCheck) -> None:
        raise ValueError("A commit delivery names the unit's delivered commit and the checked one.")

    async def checked(self, check: OrderCheck, checkout_base: Path) -> ChildResult:
        return await asyncio.to_thread(check.at, self.commit, check.directory, checkout_base)

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

    def required_claims(self, claims: tuple[ProgressClaim, ...]) -> tuple[ProgressClaim, ...]:
        return (*claims, MergedClaim(self.delivered))

    async def verified(
        self, todo: Todo, registry: Registration,
        verified_claims: dict[ProgressClaim, tuple[str, ...]],
    ) -> OrderDelivery:
        branches = verified_claims[MergedClaim(self.delivered)]
        verified = await super().verified(todo, registry, verified_claims)
        merge = await self.merge_state(todo, registry, branches)
        return CheckedCommitDelivery(commit=self.commit, delivered_commit=self.delivered,
                                     at=verified.at, evidence=self.evidence, merge=merge,
                                     proof_build=verified.proof_build)

    async def merge_state(
        self, todo: Todo, registry: Registration, branches: tuple[str, ...],
    ) -> DeliveryMerge:
        """Observe integration, not merely publication, at the time of completion.

        Git proves ordinary merges to main. The order's merged PR also proves squash merges
        and names a non-main target. Otherwise the pushed source must still have an open link,
        whose owner is WorkBranches, not a second branch policy in the delivery tool.
        """
        check = todo.order.checked().check
        if WorkBranches.main in branches:
            return MainDeliveryMerge()
        requests = await asyncio.to_thread(PullRequests((todo,)).listed,
                                           todo.repo, MergedListedPullRequest, todo)
        for request in requests:
            for merge in await request.delivery_merges(self, check):
                return merge
        linked = await asyncio.to_thread(
            WorkBranches(Path(check.directory)).read, registry.snapshot())
        sources = tuple(sorted(branch.name for branch in linked
                               if branch.name in branches and branch.linked))
        if not sources:
            raise ValueError(
                f"Delivered commit {self.delivered} is not merged and has no branch linked "
                "to an open PR or issue.")
        return UnmergedDeliveryMerge(sources)

    async def contains(self, check: OrderCheck, head: str) -> bool:
        if head == self.delivered:
            return True
        result = await BoundedRun.run(
            ("git", "merge-base", "--is-ancestor", self.delivered, head),
            cwd=check.directory, timeout=120)
        if result.outcome not in (ExitedOutcome(0), ExitedOutcome(1)):
            raise ValueError(result.stderr.decode(errors="replace") or
                             "Could not check the delivered commit against its PR head.")
        return result.outcome == ExitedOutcome(0)


class DeliveryMerge(DeclaredFamily, affix="DeliveryMerge"):
    """The integration observed when Core checked a commit delivery; never a claim of install."""

    @property
    @abstractmethod
    def line(self) -> str: ...


@dataclass(frozen=True)
class MainDeliveryMerge(DeliveryMerge):
    @property
    def line(self) -> str:
        return f"merged to {WorkBranches.main}"


@dataclass(frozen=True)
class BranchDeliveryMerge(DeliveryMerge):
    branch: str
    commit: str

    @property
    def line(self) -> str:
        return f"merged to {self.branch} as {self.commit}"


@dataclass(frozen=True)
class UnmergedDeliveryMerge(DeliveryMerge):
    branches: tuple[str, ...]

    @property
    def line(self) -> str:
        return f"not merged; linked branches: {', '.join(self.branches)}"


@dataclass(frozen=True, kw_only=True)
class CheckedCommitDelivery(CommitDelivery):
    merge: DeliveryMerge

    @property
    def line(self) -> str:
        return f"{self.work}; {self.merge.line}"


@dataclass(frozen=True, kw_only=True)
class OperationDelivery(CheckedDelivery):
    @property
    def line(self) -> str:
        return "operation checked"

    async def require_source(self, check):
        pass

    async def checked(self, check: OrderCheck, checkout_base: Path) -> ChildResult:
        return await check.run()

    async def delivery_time(self, check):
        return EventTime.capture(time.time())


@dataclass(frozen=True, kw_only=True)
class ReportDelivery(OrderDelivery):
    """The triage answer returned to the commander, not a claim of implementation."""

    report: str

    def __post_init__(self) -> None:
        if not self.report.strip():
            raise ValueError("report must contain the triage findings")

    @property
    def work(self) -> str:
        return self.report

    async def verified(
        self, todo: Todo, registry: Registration,
        verified_claims: dict[ProgressClaim, tuple[str, ...]],
    ) -> ReportDelivery:
        return replace(self, at=EventTime.capture(time.time()))


@dataclass(frozen=True)
class PullRequestLink:
    """The GitHub identity recorded by the order that delivers it."""
    repo: RepositoryName
    number: int

    def __post_init__(self) -> None:
        if FieldCodec.decode(int, self.number) <= 0:
            raise ValueError("A pull request number must be positive.")

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo.to_wire()}/pull/{self.number}"

    @staticmethod
    def _reported_number(repo: RepositoryName, reference: str) -> int:
        result = subprocess.run(
            ("gh", "pr", "view", reference, "-R", repo.to_wire(),
             "--json", "number", "--jq", ".number"),
            capture_output=True, text=True, timeout=60, check=True)
        return FieldCodec.decode(int, FieldCodec.parse(result.stdout))

    @classmethod
    def created(cls, repo: RepositoryName, reference: str) -> Self:
        """GitHub resolves its opaque creation result; we never parse its URL."""
        return cls(repo, cls._reported_number(repo, reference))

    @classmethod
    def existing(cls, repo: RepositoryName, number: int) -> Self:
        link = cls(repo, number)
        if cls._reported_number(repo, str(number)) != number:
            raise ValueError(f"GitHub did not report pull request {number} in {repo.to_wire()}.")
        return link


@dataclass(frozen=True)
class PullRequest(DeclaredFamily, affix="PullRequest"):
    number: int
    branch: str
    head: str
    updated_at: float = 0.0
    orders: tuple[str, ...] = ()
    base: str = field(default=WorkBranches.main, kw_only=True)
    problems: tuple[str, ...] = field(default=(), kw_only=True)

    def belongs_to(self, todo: Todo) -> bool:
        return (todo.id in self.problems
                or any(identity in self.orders for identity in todo.held_order_ids))

    def merged_commits(self, since: float) -> Iterator[str]:
        return iter(())

    async def delivery_merges(self, delivery: CommitDelivery,
                              check: OrderCheck) -> tuple[DeliveryMerge, ...]:
        return ()


class UnmergedPullRequest(PullRequest):
    """An open or closed request has no merge commit or integration time."""


@dataclass(frozen=True, kw_only=True)
class MergedPullRequest(PullRequest):
    merge: str
    merged_at: float

    def merged_commits(self, since: float) -> Iterator[str]:
        if self.merged_at >= since:
            yield self.merge

    async def delivery_merges(self, delivery: CommitDelivery,
                              check: OrderCheck) -> tuple[DeliveryMerge, ...]:
        if not await delivery.contains(check, self.head):
            return ()
        return (MainDeliveryMerge() if self.base == WorkBranches.main
                else BranchDeliveryMerge(self.base, self.merge),)


def _github_time(text: str) -> float:
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


@dataclass(frozen=True)
class PullRequestCommit:
    """GitHub's mergeCommit object on a merged request."""
    oid: str


@dataclass(frozen=True)
class ListedPullRequest(DeclaredFamily, affix="ListedPullRequest"):
    """The external query row; conversion lives here, never in its consumers."""
    number: int
    branch: str
    head: str
    updated: str
    base: str

    def parsed(self, problems: tuple[str, ...], orders: tuple[str, ...]) -> PullRequest:
        return UnmergedPullRequest(self.number, self.branch, self.head, _github_time(self.updated),
                                   orders, base=self.base, problems=problems)


class UnmergedListedPullRequest(ListedPullRequest):
    """An open request, selected by its declared member rather than a state string."""


class ClosedListedPullRequest(ListedPullRequest):
    """Closed without merging; never selected as an open request."""


@dataclass(frozen=True, kw_only=True)
class MergedListedPullRequest(ListedPullRequest):
    merge: PullRequestCommit
    merged: str

    def parsed(self, problems: tuple[str, ...], orders: tuple[str, ...]) -> MergedPullRequest:
        return MergedPullRequest(self.number, self.branch, self.head, _github_time(self.updated),
                                 orders, base=self.base,
                                 merge=self.merge.oid, merged_at=_github_time(self.merged),
                                 problems=problems)


class PullRequests:
    """GitHub facts for recorded links, cached within one board read."""

    def __init__(self, todos: Sequence[Todo] = ()) -> None:
        self._read: dict[tuple[RepositoryName, type[ListedPullRequest], str], tuple[PullRequest, ...]] = {}
        self._todos: dict[RepositoryName, dict[str, Todo]] = {}
        for todo in todos:
            self._todos.setdefault(todo.repo, {})[todo.id] = todo

    def listed(self, repo: RepositoryName, kind: type[ListedPullRequest],
               todo: Todo) -> tuple[PullRequest, ...]:
        key = (repo, kind, todo.id)
        if key not in self._read:
            self._read[key] = self.read(repo, kind, todo)
        return tuple(request for request in self._read[key] if request.belongs_to(todo))

    def read(self, repo: RepositoryName, kind: type[ListedPullRequest],
             todo: Todo) -> tuple[PullRequest, ...]:
        problems = self._todos.setdefault(repo, {})
        problems[todo.id] = todo
        linked: dict[int, list[Todo]] = {}
        for candidate in problems.values():
            for number in {link.number for order in candidate.order_history
                           for link in order.pull_requests if link.repo == repo}:
                linked.setdefault(number, []).append(candidate)
        if not linked:
            for candidate in problems.values():
                self._read[repo, kind, candidate.id] = ()
            return ()
        selections = [
            f'p{number}: pullRequest(number: {number}) {{ number branch: headRefName '
            'head: headRefOid base: baseRefName closed merge: mergeCommit { oid } '
            'merged: mergedAt updated: updatedAt }' for number in linked]
        query = (f'query {{ repository(owner: {json.dumps(repo.owner)}, '
                 f'name: {json.dumps(repo.name)}) {{ {" ".join(selections)} }} }}')
        listed = subprocess.run(
            ("gh", "api", "graphql", "-f", "query=" + query,
             "--jq", '.data.repository | map_values((if .merged == null then '
             '(if .closed then '
             f'del(.merge,.merged) + {{kind: "{ClosedListedPullRequest.declared_name}"}} else '
             f'del(.merge,.merged) + {{kind: "{UnmergedListedPullRequest.declared_name}"}} end) else '
             f'. + {{kind: "{MergedListedPullRequest.declared_name}"}} end) | del(.closed))'),
            capture_output=True, text=True, timeout=60)
        if listed.returncode:
            raise WorkBranchError(
                listed.stderr.strip() or f"GitHub order lookup in {repo.to_wire()} failed.")
        rows = FieldCodec.decode(dict[str, ListedPullRequest], FieldCodec.parse(listed.stdout))
        requests = tuple(row.parsed(
            tuple(candidate.id for candidate in linked[row.number]),
            tuple(dict.fromkeys(order.id for candidate in linked[row.number]
                                for order in candidate.order_history
                                if PullRequestLink(repo, row.number) in order.pull_requests)))
            for row in rows.values() if isinstance(row, kind))
        for candidate in problems.values():
            self._read[repo, kind, candidate.id] = tuple(sorted(
                (request for request in requests if request.belongs_to(candidate)),
                key=lambda request: request.updated_at, reverse=True))
        return self._read[repo, kind, todo.id]


@dataclass(frozen=True)
class ProgressPoint:
    """A revision and its time in the order's progress; Git rows use committer time."""
    sha: str
    at: float
    format: ClassVar[str] = "%H %ct"

    @classmethod
    def listed(cls, line: str) -> ProgressPoint:
        sha, at = line.split()
        return cls(sha, float(at))


@dataclass(frozen=True)
class DistanceReading:
    at: float
    distance: CheckPrinted
    command: str = ""


@dataclass(frozen=True)
class DistanceReadings:
    """The order's last two distance readings, oldest first: its base at the order, then its
    newest commit. Fewer than two read, or a last reading at done, project nothing."""
    readings: tuple[DistanceReading, ...] = ()
    window: ClassVar[int] = 2

    @property
    def trend(self) -> DistanceTrend:
        """A missing count diagnoses the check; only measured counts project a finish."""
        if not self.readings:
            return UnreadTrend()
        return self.readings[-1].distance.project(
            self.counted_trend, lambda printed: UncountedTrend(printed, self.readings[-1].command))

    def counted_trend(self, final: int) -> DistanceTrend:
        last = self.readings[-self.window:]
        if len(last) < self.window or not final:
            return UnreadTrend()
        first, latest = last

        def compared(previous: int) -> DistanceTrend:
            if final >= previous:
                return FlatTrend(final, latest.at)
            rate = (previous - final) / max(latest.at - first.at, 1.0)
            return MovingTrend(final, latest.at + final / rate)

        return first.distance.project(compared, lambda printed: UnreadTrend())


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
        return f"; distance {self.distance}, projected done {RecordedEventTime(self.done).clock()}"


@dataclass(frozen=True)
class FlatTrend(DistanceTrend):
    """The distance did not fall from the reading before ``since``."""

    distance: int
    since: float

    @property
    def projection(self) -> str:
        return f"; distance {self.distance}, not falling"


@dataclass(frozen=True)
class UncountedTrend(DistanceTrend):
    printed: NoIntegerPrinted
    command: str

    @property
    def projection(self) -> str:
        return self.printed.progress(self.command)


@dataclass(frozen=True)
class UnreadTrend(DistanceTrend):
    """Fewer than two readings, or the last one at done: nothing to project from."""

    @property
    def projection(self) -> str:
        return ""


@dataclass(frozen=True)
class ProgressCheckout(DeclaredFamily, affix="Checkout"):
    """The local history available when an order's progress is captured."""
    path: Path

    @abstractmethod
    def commits(self, pattern: str, refs: tuple[str, ...],
                *excluded: str) -> Iterator[ProgressPoint]: ...

    @abstractmethod
    def references(self, storage: StorageRule, owner: Thread) -> tuple[str, ...]: ...

    @abstractmethod
    def readings(self, source: ProgressSource) -> DistanceReadings: ...

    @abstractmethod
    def pushes(self, pattern: str) -> Iterator[tuple[str, float]]: ...

    def checked_commits(self, order: CheckedProblemOrder, refs: tuple[str, ...],
                        *excluded: str) -> Iterator[ProgressPoint]:
        return iter(())

    def published(self, pattern: str) -> Iterator[ProgressPoint]:
        return iter(())


class MissingCheckout(ProgressCheckout):
    """A retired checkout contributes no local history, not a failed Git read."""

    def commits(self, pattern: str, refs: tuple[str, ...],
                *excluded: str) -> Iterator[ProgressPoint]:
        return iter(())

    def references(self, storage: StorageRule, owner: Thread) -> tuple[str, ...]:
        return ()

    def readings(self, source: ProgressSource) -> DistanceReadings:
        return DistanceReadings()

    def pushes(self, pattern: str) -> Iterator[tuple[str, float]]:
        return iter(())


class PresentCheckout(ProgressCheckout):
    default_reference: ClassVar[str] = "refs/remotes/origin/HEAD"

    @property
    def default_refs(self) -> tuple[str, ...]:
        return (self.default_reference,) if self.default_reference in self.git_refs else ()

    def git(self, *args: str) -> str:
        return CheckedCommit.git(str(self.path), *args)

    @cached_property
    def git_refs(self) -> tuple[str, ...]:
        return tuple(self.git("for-each-ref", "--format=%(refname)",
                              "refs/heads/", "refs/remotes/origin/").splitlines())

    @property
    def origin_refs(self) -> tuple[str, ...]:
        return tuple(ref for ref in self.git_refs if ref.startswith("refs/remotes/origin/"))

    def references(self, storage: StorageRule, owner: Thread) -> tuple[str, ...]:
        owned = {f"refs/heads/{branch}"
                 for branch, paths in WorkBranches(self.path).worktrees().items()
                 if any(storage.owns_checkout(owner, Path(path).resolve()) for path in paths)}
        return (*(ref for ref in self.git_refs if ref in owned), *self.origin_refs)

    def commits(self, pattern: str, refs: tuple[str, ...],
                *excluded: str) -> Iterator[ProgressPoint]:
        """Newest problem commit across owned branches and retained origin references."""
        if refs:
            for line in self.git("log", "-1", "--extended-regexp", "--regexp-ignore-case",
                                 f"--grep={pattern}", f"--format={ProgressPoint.format}",
                                 *refs, "--not", *excluded).splitlines():
                yield ProgressPoint.listed(line)

    def checked_commits(self, order: CheckedProblemOrder, refs: tuple[str, ...],
                        *excluded: str) -> Iterator[ProgressPoint]:
        """A recorded check links a head even without a problem token in its title."""
        return (point for point in self.commits("", refs, *excluded)
                if order.check.recorded(point.sha, str(self.path)))

    def published(self, pattern: str) -> Iterator[ProgressPoint]:
        return self.commits(pattern, self.origin_refs, *self.default_refs)

    def edits(self) -> Iterator[float]:
        """When each uncommitted change in this checkout was last written; a deletion has none."""
        return (path.stat().st_mtime
                for path in WorkBranches(self.path).read_checkout().uncommitted()
                if path.exists())

    def readings(self, source: ProgressSource) -> DistanceReadings:
        """Read bounded recorded results; an absent remote default supplies no invented base."""
        if not self.default_refs:
            return DistanceReadings()
        readings: list[DistanceReading] = []
        for tip in source.commits(*self.default_refs):
            base = self.git("merge-base", tip.sha, self.default_reference).strip()
            commits = [ProgressPoint.listed(line) for line in reversed(self.git(
                "log", f"--max-count={DistanceReadings.window}", "--first-parent",
                f"--format={ProgressPoint.format}", tip.sha, "--not", base).splitlines())]
            points = [ProgressPoint(base, source.since), *commits][-DistanceReadings.window:]
            readings.extend(reading for point in points for reading in self.recorded_distances(
                source.todo.order.checked(), base, point))
        return DistanceReadings(tuple(readings))

    def recorded_distances(self, order: CheckedProblemOrder, base: str,
                           point: ProgressPoint) -> Iterator[DistanceReading]:
        """Read the check run result; without a recorded result the distance is unknown."""
        for run in order.check.recorded(point.sha, str(self.path)):
            touched = self.git("diff", "--name-only", base, point.sha).split()
            yield DistanceReading(point.at, order.check.result.distance(run).plus(
                order.untouched(touched)), order.check.command)

    def pushes(self, pattern: str) -> Iterator[tuple[str, float]]:
        """Retained origin commits prove publication; their push reflog supplies its clock."""
        published = {point.sha for point in self.published(pattern)}
        for row in self.git("reflog", "show", "--all", "--date=unix",
                            "--grep-reflog=update by push", "--format=%gD%x09%H").splitlines():
            selector, sha = row.split("\t")
            reference, stamp = selector.rsplit("@{", 1)
            if reference in self.origin_refs and sha in published:
                yield sha, float(stamp.removesuffix("}"))


@dataclass(frozen=True)
class ProgressSource:
    """Problem progress across owned worktrees, shared Git history and retained PR identities."""
    todo: Todo
    checkout: ProgressCheckout
    pulls: PullRequests
    references: tuple[str, ...] = ()
    worktrees: tuple[PresentCheckout, ...] = ()

    @classmethod
    def of(cls, todo: Todo, registry: Registration, pulls: PullRequests) -> Self:
        owner = registry.require(todo.held().owner)
        storage = registry.storage_rule
        owned = storage.checkout_paths(owner)
        candidates = (Path(todo.order.checked().check.directory), *owned)
        path = next((path for path in candidates if (path / ".git").exists()),
                    next(iter(owned), candidates[0]))
        checkout = PresentCheckout(path) if (path / ".git").exists() else MissingCheckout(path)
        return cls(todo, checkout, pulls, checkout.references(storage, owner),
                   tuple(PresentCheckout(path) for path in owned if (path / ".git").exists()))

    @property
    def since(self) -> float:
        return self.todo.order.checked().at.recorded_at("An order")

    def requests(self, kind: type[ListedPullRequest]) -> list[PullRequest]:
        """Explicit PR references survive checkout removal or branch reuse."""
        return list(self.pulls.listed(self.todo.repo, kind, self.todo))

    def commits(self, *excluded: str) -> Iterator[ProgressPoint]:
        points = (
            *self.checkout.checked_commits(self.todo.order.checked(), self.references, *excluded),
            *self.checkout.commits(self.todo.reference_pattern, self.references, *excluded),
        )
        return iter(sorted({point.sha: point for point in points}.values(),
                           key=lambda point: point.at, reverse=True)[:1])

    def published(self) -> Iterator[ProgressPoint]:
        return self.checkout.published(self.todo.reference_pattern)

    @cached_property
    def pushes(self) -> tuple[PushedProgress, ...]:
        return tuple(PushedProgress(sha[:9], at=at)
                     for sha, at in self.checkout.pushes(self.todo.reference_pattern))

    @cached_property
    def edited_at(self) -> tuple[float, ...]:
        """The newest uncommitted change in the holder's own checkouts: work no commit shows yet."""
        return tuple(sorted((edited for worktree in self.worktrees for edited in worktree.edits()),
                            reverse=True)[:1])

    @cached_property
    def moved_at(self) -> float:
        return max((self.since, *(commit.at for commit in self.commits()),
                    *(push.at for push in self.pushes), *self.edited_at))

    def readings(self) -> DistanceReadings:
        """Progress observation does not run the order's Check."""
        return DistanceReadings()


class MeasuredProgressSource(ProgressSource):
    """The board reads recorded distances; merge and install reads need none."""

    def readings(self) -> DistanceReadings:
        return self.checkout.readings(self)


@dataclass(frozen=True)
class OrderProgress(DeclaredFamily, affix="Progress"):
    """Where an order's work stands, derived on each read from its unit's checkout and PRs.

    Never stored: the checkout and GitHub are its owners."""

    @classmethod
    def read(cls, todo: Todo, registry: Registration, pulls: PullRequests) -> OrderProgress:
        """The first found member by its declared precedence; with none found, no commit yet."""
        return cls.from_source(MeasuredProgressSource.of(todo, registry, pulls))

    @classmethod
    def from_source(cls, source: ProgressSource) -> OrderProgress:
        members = sorted(cls.members_with(FoundProgress), key=lambda member: member.precedence)
        try:
            return next((progress for member in members
                         for progress in member.observations(source)), NoCommitProgress())
        except (WorkBranchError, subprocess.TimeoutExpired) as unread:
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

    @classmethod
    def observations(cls, source: ProgressSource) -> Iterator[Self]:
        return cls.found(source)


@dataclass(frozen=True)
class Moving:
    """Work whose last edit, commit, push or PR change was observed at ``at``."""
    at: float = field(kw_only=True)
    readings: DistanceReadings = field(default=DistanceReadings(), kw_only=True, compare=False)

    def distance_readings(self) -> DistanceReadings:
        return self.readings


@dataclass(frozen=True)
class NoCommitProgress(OrderProgress):
    def missing_merges(self, source: ProgressSource) -> tuple[MergedProgress, ...]:
        return tuple(MergedProgress.found(source))

    @property
    def state(self) -> str:
        return "no commit since the order"


@dataclass(frozen=True)
class EditingProgress(Moving, NoCommitProgress, FoundProgress):
    """Uncommitted changes in the holder's own checkout: work is moving, nothing is delivered."""
    precedence: ClassVar[int] = 6

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(at=edited) for edited in source.edited_at)

    @property
    def state(self) -> str:
        return f"uncommitted edits at {RecordedEventTime(self.at).clock()}, no commit"


@dataclass(frozen=True)
class UnreadProgress(OrderProgress):
    reason: str

    @property
    def state(self) -> str:
        return f"progress unread: {self.reason}"


class GitProgress(Moving, FoundProgress, declared_name=None):
    """Found progress whose activity includes the checkout's commits and pushes."""

    @classmethod
    def observations(cls, source: ProgressSource) -> Iterator[Self]:
        return (replace(progress, at=max(progress.at, source.moved_at))
                for progress in cls.found(source))


@dataclass(frozen=True)
class CommittedProgress(GitProgress):
    precedence: ClassVar[int] = 4
    sha: str

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(commit.sha[:9], at=commit.at, readings=source.readings())
                for commit in source.commits("--remotes"))

    @property
    def state(self) -> str:
        return f"committed {self.sha} at {RecordedEventTime(self.at).clock()}, not pushed"


@dataclass(frozen=True)
class PushedProgress(CommittedProgress):
    """Pushed past the default branch, with no PR naming the order yet."""
    precedence: ClassVar[int] = 5

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        moves = (*source.published(),
                 *(ProgressPoint(push.sha, push.at) for push in source.pushes))
        return (cls(move.sha[:9], at=move.at, readings=source.readings())
                for move in sorted(moves, key=lambda move: move.at, reverse=True)[:1])

    @property
    def state(self) -> str:
        return f"pushed {self.sha} at {RecordedEventTime(self.at).clock()}, no PR"


@dataclass(frozen=True)
class OpenPrProgress(GitProgress):
    precedence: ClassVar[int] = 3
    number: int
    head: str

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(pr.number, pr.head[:9], at=pr.updated_at,
                    readings=source.readings()) for pr in source.requests(UnmergedListedPullRequest))

    @property
    def state(self) -> str:
        return f"PR #{self.number} open at {self.head}"


@dataclass(frozen=True)
class MergedProgress(FoundProgress):
    """The forge proves a linked merge; the order's run owns what still needs doing."""
    precedence: ClassVar[int] = 1
    sha: str

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(sha) for pr in source.requests(MergedListedPullRequest)
                for sha in pr.merged_commits(source.since))

    @property
    def state(self) -> str:
        return f"merged as {self.sha[:9]}"

    def waiting_on_install(self, todo: Todo) -> tuple[Todo, ...]:
        return (todo,)


@dataclass(frozen=True)
class OrderWords(WireValue):
    """Text the commander writes; quoted, backticked, indented and path-like spans are someone else's words."""

    # A path/file match starts at a token boundary or immediately after a
    # quoted span. Retrying its greedy prefix at every character is quadratic
    # for long ordinary words with no slash or filename suffix.
    CITED: ClassVar[re.Pattern] = re.compile(
        r'(?m)^ {4}.*$|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|'
        r'(?:(?<!\S)|(?<=[`"”]))(?:\S*/\S*|\S+\.(?:py|md|jsonl?|txt|png|toml|ts|log|patch)\b)|'
        r'\b\w+_\w+\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b')  # identifiers are code, not prose
    REFUSED: ClassVar[re.Pattern]
    refusal: ClassVar[str]
    least_words: ClassVar[int] = 1
    text: str

    def __post_init__(self) -> None:
        if refused := self.refused(self.text):
            raise ToolRefusedError(refused)

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

    @staticmethod
    def refuses(found: list[str]) -> bool:
        """Flags, never blocks: a wording slip costs a note, not a resend."""
        if found:
            print(f"Flag: jargon {', '.join(found)} hides what happened; say it plainly.", file=sys.stderr)
        return False


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


class BluntWords:
    """The words that name witnessed incompetence, and the refusal a text lacking them gets."""

    REFUSED: ClassVar[re.Pattern] = re.compile(
        r"\b(stupid|slop|bullshit|useless|wrong|lazy|garbage|nonsense|incompeten\w*|sloppy|theater|dumb|"
        r"worthless|broke|broken|pointless|waste)\b", re.I)
    refusal: ClassVar[str] = "Not blunt: name what was stupid/slop/wrong and why, or pass --clean if nothing was."


class BluntText(BluntWords, OrderWords):
    """Witnessed incompetence is named as such: the text must call something stupid, slop or wrong."""

    @staticmethod
    def refuses(found: list[str]) -> bool:
        return not found


class FairText(OrderWords):
    """Paper 1 proves multiple parents derive every required pair with nothing outside, which composition and
    forwarding supply outside (Fig 3, Thm 4.6): faulting inheritance itself is dogma, not a restated fact.
    Only explanatory prose is checked; code and banned-action fields are not prose."""

    REFUSED = re.compile(r"composition over inheritance|(prefer|favou?r|use) composition|inherits (two|both|multiple|several|\d+)\b"
                         r"|multiple (inheritance|parents) (is (bad|wrong|dangerous|confusing|fragile|slop)|causes|creates|smells|complicates)"
                         r"|diamond problem|fragile base|(avoid|no|stop using|drop) (inheritance|mixins?|multiple (parents|inheritance))"
                         r"|too many (parents|base classes|mixins)|deep (inheritance|hierarch)|mixing (of )?\w+ and \w+"
                         r"|inheritance (abuse|soup|hell|is wrong)", re.I)
    refusal = ("{1} faults inheritance itself. Cite a refactor-audit pattern ID, "
               "name the restated fact and the declaration that derives it.")


class HierarchyText(OrderWords):
    """A forwarder restates an implementation pair: explanatory prose must fix the
    hierarchy rather than preserve a forwarding route."""

    REFUSED = re.compile(r"(leave|keep|retain|preserve)[^.\n]{0,60}\bforward|forwarders? (stay|remain)|reach(es|ing)? through", re.I)
    refusal = ("{1} keeps a forwarder or bans fixing the hierarchy: fold the false owner into the real one or compose "
               "providers as parents, and delete every route.")


class Derivation(DeclaredFamily, affix="Derivation"):
    sentence: ClassVar[str]

    @classmethod
    def declaration_elsewhere(cls, by: FileLine, where: FileLine, root: Path) -> bool:
        return False


class DeclarationDerivation(Derivation):
    sentence = "the declaration derives it"

    @classmethod
    def declaration_elsewhere(cls, by: FileLine, where: FileLine, root: Path) -> bool:
        return not by.same_file(where, root)


class NominalKeyDerivation(Derivation, declared_name="nominal-key"):
    sentence = "the nominal key identifies it"


class SubclassDerivation(DeclarationDerivation):
    sentence = "the subclass derives it"


class MixinDerivation(DeclarationDerivation):
    sentence = "the mixed-in capability derives it"


class BaseMethodDerivation(DeclarationDerivation, declared_name="base-method"):
    sentence = "the base method derives it"


class InitSubclassDerivation(DeclarationDerivation, declared_name="init-subclass"):
    sentence = "subclass initialization derives it"


class MetaclassDerivation(DeclarationDerivation):
    sentence = "the metaclass derives it"


class DeleteCopyDerivation(Derivation, declared_name="delete-copy"):
    sentence = "deleting the copy leaves its original declaration"


@dataclass(frozen=True)
class Slop:
    pattern: str
    fact: str
    at: FileLine
    derive: type[Derivation]
    by: FileLine

    PATTERN_ENTRY: ClassVar[re.Pattern] = re.compile(r'^## ([A-Z]+-\d+)\s+\S', re.M)

    @staticmethod
    def catalog() -> Path:
        """The pinned refactor-audit pattern catalog that slop citations name."""
        return Path(str(files(skills) / 'refactor-audit' / 'references' / 'patterns'))

    @classmethod
    @cache
    def patterns(cls) -> frozenset[str]:
        return frozenset(found[1] for source in cls.catalog().glob('*.md')
                         for found in cls.PATTERN_ENTRY.finditer(source.read_text(encoding='utf-8')))

    def __post_init__(self):
        problems = []
        if self.pattern not in self.patterns():
            problems.append(f'Unknown refactor-audit pattern ID {self.pattern!r}; '
                            'fix: use an ID from the packaged pattern catalog.')
        if len(self.fact.split()) < 4:
            problems.append('fact: fix: describe the duplicated fact in at least four words')
        if problems:
            raise ValueError('\n'.join(problems))

    def render(self):
        return (f'{self.at.to_wire()} restates {self.pattern}: {self.fact}. '
                f'{self.by.to_wire()} owns it; {self.derive.sentence}, so the copy is deleted.')


class DirectiveText:
    """What the commander and the owner's voice send units: order send, order say and the
    connector's send refuse a text failing any of these."""

    OUTGOING: ClassVar[tuple[type[OrderWords], ...]] = (
        FairText, HierarchyText, PlainText)

    @classmethod
    def refused(cls, text: str) -> tuple[str, ...]:
        return tuple(refused for family in cls.OUTGOING if (refused := family.refused(text)))

    @classmethod
    def require(cls, text: str) -> None:
        if refused := cls.refused(text):
            raise ToolRefusedError("Directive refused, nothing sent: " + " ".join(refused))




class OrderDeletion(DeclaredFamily, affix="Deletion"):
    """The deletion an order declares, decoded as its own fields."""

    label: ClassVar[str] = "Deletes:"
    checks: ClassVar[tuple[OrderCheck, ...]] = ()

    @property
    def modules(self) -> tuple[str, ...]:
        return ()

    def instructions(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class NoOrderDeletion(OrderDeletion):
    pass


@dataclass(frozen=True)
class ModuleDeletion(OrderDeletion):
    modules: tuple[str, ...] = ()

    def instructions(self) -> tuple[str, ...]:
        return (f"{self.label} {', '.join(self.modules)}",)


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


@dataclass(frozen=True)
class ProblemFinding(DeclaredFamily, affix="ProblemFinding"):
    def require_references(self, root: Path, revision: SourceRevision) -> None:
        """An unlocated finding has no source reference to validate."""

    def on_files(self, locations: tuple[FileLine, ...], root: Path) -> bool:
        return False

    def instructions(self) -> tuple[str, ...]:
        return ()

    def place(self) -> str:
        return "-"


@dataclass(frozen=True)
class UnlocatedProblemFinding(ProblemFinding):
    pass


@dataclass(frozen=True)
class LocatedProblemFinding(ProblemFinding):
    where: FileLine

    def require_references(self, root: Path, revision: SourceRevision) -> None:
        self.where.require_exists(root, revision)

    def on_files(self, locations: tuple[FileLine, ...], root: Path) -> bool:
        return any(self.where.same_file(location, root) for location in locations)

    def instructions(self) -> tuple[str, ...]:
        return (f"Where: {self.place()}",)

    def place(self) -> str:
        return self.where.to_wire()


@dataclass(frozen=True, kw_only=True)
class Successor(SubclassUnion, affix="Successor"):
    """The problem that already owns a withdrawn order's fix, or none."""

    @abstractmethod
    def withdrawal_note(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SupersedingProblem(SingleFieldValue, Successor):
    problem: str

    def withdrawal_note(self) -> str:
        return f" (superseded by {self.problem})"


@dataclass(frozen=True, slots=True)
class NoSuccessor(NullMember, Successor):
    def withdrawal_note(self) -> str:
        return ""


@dataclass(frozen=True, kw_only=True)
class ProblemOrder(DeclaredFamily, StorySource, affix="ProblemOrder"):
    """A problem's order state; an unassigned finding is not a fabricated order."""

    pending: ClassVar[bool] = False

    @property
    def line(self) -> str:
        return "No order"

    def corrected(self, correction: CorrectCheckTodo) -> ProblemOrder:
        """Only pending and claimed orders take a check correction (DeliverableProblemOrder)."""
        raise ValueError("Only a pending or claimed order takes a check correction.")

    finding: Annotated[ProblemFinding, WireOmitDefault()] = field(
        default_factory=UnlocatedProblemFinding
    )

    @property
    def declarations(self) -> tuple[CheckedProblemOrder, ...]:
        return ()

    def closed_by_merge(self, todo: Todo, registry: Registration,
                        pulls: PullRequests) -> tuple[Todo, ...]:
        return ()

    def reissued(self, at: float) -> PendingProblemOrder:
        """This order sent again at ``at`` to a new owner, its first-call window kept."""
        raise ValueError("Only an undelivered order can change owner.")

    def board_cells(self, todo: Todo) -> str:
        return f"{self.finding.place()} | unassigned | - | - | -"

    def checked(self) -> CheckedProblemOrder:
        raise ValueError("This problem has no checked order.")

    @property
    def phase(self) -> type[OrderPhase]:
        return UnplannedPhase

    def for_phase(self, phase: type[OrderPhase]) -> ProblemOrder:
        raise ValueError("Only a checked order can declare a plan phase.")

    @property
    def accepted(self) -> bool:
        return False

    def require_transfer(self, todo: Todo) -> None:
        raise TodoError("This problem cannot receive another order.")

    def require_reassignment(self, todo: Todo, proposed: Assignment) -> None:
        """Every reassignment is a transfer unless the order state says otherwise."""
        self.require_transfer(todo)

    def require_close(self):
        raise ValueError("Closing requires recorded installed application evidence.")

    def require_installation(self) -> None:
        raise ValueError("Check the delivered order before recording installation.")

    def require_withdrawal(self) -> None:
        raise ValueError("Only an undelivered order can be withdrawn.")

    def withdrawals(self, todo: Todo) -> tuple[str, ...]:
        return ()

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return ()

    def closing_artifact(self) -> str:
        raise ValueError("Only a delivered order has closing evidence.")

    def linked(self, order: PendingProblemOrder) -> PendingProblemOrder:
        raise ValueError("This problem already has an order; drop it before linking another.")

    def folded(self, into: str, why: str) -> ProblemOrder:
        if self.pending:
            raise ValueError("The problem has open orders; link or drop them first.")
        return FoldedProblemOrder(original=self, into=into, why=why)

    @staticmethod
    def request(sender: Thread, problems: tuple[str, ...]) -> str:
        if not problems:
            raise ValueError('An order needs its Problems line.')
        request = sender.goal.require().require_active()
        request.order.require_request()
        return request.id


@dataclass(frozen=True, kw_only=True)
class UnorderedProblemOrder(ProblemOrder):

    def require_transfer(self, todo: Todo) -> None:
        pass

    def instructions(self, todo: Todo) -> str:
        return "\n".join((f"Problem: {todo.id}: {todo.text}",
                          *self.finding.instructions(), "No order"))

    def linked(self, order: PendingProblemOrder) -> PendingProblemOrder:
        return replace(order, finding=self.finding)


@dataclass(frozen=True, kw_only=True)
class FoldedProblemOrder(ProblemOrder):
    original: ProblemOrder
    into: str
    why: str
    line: ClassVar[str] = "Folded"

    @property
    def phase(self) -> type[OrderPhase]:
        return self.original.phase

    @property
    def declarations(self) -> tuple[CheckedProblemOrder, ...]:
        return self.original.declarations

    def withdrawals(self, todo: Todo) -> tuple[str, ...]:
        return (f"{todo.id} folded into {self.into}: {self.why}",)

    def story(self) -> tuple[StoryLine, ...]:
        return self.original.story()


@dataclass(frozen=True)
class FixShape:
    """The parent declarations owning a fix and the related member declarations."""

    parents: tuple[FileLine, ...]
    members: tuple[FileLine, ...]

    def __post_init__(self) -> None:
        if not self.parents:
            raise ValueError("scope.shape.parents must name at least one parent class declaration")

    def require_references(self, root: Path, revision: SourceRevision) -> None:
        for location in (*self.parents, *self.members):
            location.require_class(root, revision)


@dataclass(frozen=True)
class OrderScope[Delivery: OrderDelivery](DeclaredFamily, affix="Scope"):
    """What the commander authorizes, including the delivery type at its boundary."""

    delivery_class: ClassVar[type[OrderDelivery]]

    def __init_subclass__(cls, **options):
        super().__init_subclass__(**options)
        cls.delivery_class = next(
            get_args(base)[0]
            for owner in cls.__mro__
            for base in get_original_bases(owner)
            if get_origin(base) is OrderScope
        )

    def require_references(self, root: Path, revision: SourceRevision) -> None:
        """Triage has no implementation declarations to validate."""

    @abstractmethod
    def instructions(self, problem: str) -> tuple[str, ...]: ...

    @abstractmethod
    def verified_claims(
        self, delivery: Delivery, claims: tuple[ProgressClaim, ...],
        comms: Comms, checkout: Path,
    ) -> dict[ProgressClaim, tuple[str, ...]]: ...

    @abstractmethod
    def completed(self, todo: Todo, delivery: Delivery) -> Todo: ...


class TriageScope(OrderScope[ReportDelivery]):
    def instructions(self, problem: str) -> tuple[str, ...]:
        return ("Scope: triage; classify and report only. Do not edit source or deliver commits.",
                "Return the report to your commander; do not open an implementation PR.")

    def verified_claims(
        self, delivery: ReportDelivery, claims: tuple[ProgressClaim, ...],
        comms: Comms, checkout: Path,
    ) -> dict[ProgressClaim, tuple[str, ...]]:
        if claims:
            raise ValueError("scope triage returns a report, not merged, installed or working claims")
        return {}

    def completed(self, todo: Todo, delivery: ReportDelivery) -> Todo:
        order = todo.order.checked().copy_as(ReportedProblemOrder, delivery=delivery)
        return DoneTodoState.closed(todo, order)


@dataclass(frozen=True)
class FixScope(OrderScope[CheckedDelivery]):
    shape: FixShape

    def require_references(self, root: Path, revision: SourceRevision) -> None:
        self.shape.require_references(root, revision)

    def instructions(self, problem: str) -> tuple[str, ...]:
        return ("Scope: fix", "Parents: " + ", ".join(p.to_wire() for p in self.shape.parents),
                "Members: " + ", ".join(m.to_wire() for m in self.shape.members),
                f"Open the PR with agent-comms-commander order pr {problem} "
                "--title '<title>' --body-file <file>; Core records its link on this order.",
                "Record the unit's delivery claim in Core before waiting for the commander's "
                "verdict; a chat report does not change the order.")

    def verified_claims(
        self, delivery: CheckedDelivery, claims: tuple[ProgressClaim, ...],
        comms: Comms, checkout: Path,
    ) -> dict[ProgressClaim, tuple[str, ...]]:
        ProgressClaim.require_claimed(claims, "delivered")
        return ProgressClaim.require_all(delivery.required_claims(claims), comms, checkout)

    def completed(self, todo: Todo, delivery: CheckedDelivery) -> Todo:
        return replace(todo, order=todo.order.delivered(delivery), revision=todo.revision + 1)


@dataclass(frozen=True, kw_only=True)
class CheckedProblemOrder(ProblemOrder, declared_name=None):
    """A checked order keeps its actual body, check and source location."""

    @property
    def installation_commits(self) -> tuple[str, ...]:
        return ()

    @property
    def installation_build(self) -> BuildIdentity:
        return RecordedBuildIdentity.installed()

    def installation(self) -> CommitInstallation:
        return self.installation_build.installation(
            self.installation_commits, Path(self.check.directory)
        )

    def require_installation(self) -> None:
        self.installation().require()

    def claimed(self, claim: OrderClaim) -> ClaimedProblemOrder:
        raise TodoError("Only a pending order can be claimed by its holder.")

    def sent_back(self, reason: StrikeReason) -> CheckedProblemOrder:
        return self

    def on_files(self, locations: tuple[FileLine, ...], root: Path) -> bool:
        return (any(self.owner.same_file(location, root) for location in locations)
                or self.finding.on_files(locations, root))

    def checked(self) -> CheckedProblemOrder:
        return self

    @cached_property
    def cited_revision(self) -> SourceRevision:
        return (*map(PullRequestHeadRevision, self.pull_requests), MainRevision())[0]

    def require_references(self) -> None:
        root, revision = Path(self.check.directory), self.cited_revision
        self.owner.require_exists(root, revision)
        self.finding.require_references(root, revision)
        self.scope.require_references(root, revision)

    def require_wording(self) -> None:
        """Check fresh order prose, never reinterpret stored history or shell commands."""
        for text in (*self.edits, *self.banned, self.report, *self.run.instructions()):
            DirectiveText.require(text)

    def story(self) -> tuple[StoryLine, ...]:
        return (StoryLine(self.at, f"Ordered {self.id}: {'; '.join(self.edits)}"),)

    @property
    def declarations(self) -> tuple[CheckedProblemOrder, ...]:
        return (self,)

    id: str
    pull_requests: Annotated[tuple[PullRequestLink, ...], WireOmitDefault()] = ()
    owner: FileLine
    scope: OrderScope
    check: OrderCheck
    edits: tuple[str, ...]
    banned: tuple[str, ...]
    report: str
    at: EventTime
    first_call_deadline: EventTime
    check_corrections: Annotated[
        tuple[CorrectCheckTodo, ...], WireOmitDefault()
    ] = ()
    run: OrderRun
    deletes: Annotated[OrderDeletion, WireOmitDefault()] = field(
        default_factory=NoOrderDeletion
    )
    serves: Annotated[type[OrderPhase], WireOmitDefault()] = UnplannedPhase

    def board_cells(self, todo: Todo) -> str:
        last = todo.released[-1:]
        owner = todo.holder or (f"unassigned: released from {last[0].previous.owner}: "
                                f"{last[0].reason.text}")
        check = json.dumps(FieldCodec.encode(self.check)).replace("|", "&#124;")
        return f"{self.owner.to_wire()} | {owner} | {self.id} | {todo.review_at.clock()} | {check}"

    @property
    def phase(self) -> type[OrderPhase]:
        return self.serves

    def for_phase(self, phase: type[OrderPhase]) -> CheckedProblemOrder:
        return replace(self, serves=phase)

    def __post_init__(self):
        problems = []
        if not self.edits or not self.banned:
            problems.append("edits and banned: fix: supply nonempty arrays of actions")
        if any(not text.strip() for text in (self.id, self.report, *self.edits, *self.banned)):
            problems.append("order text is empty; fix: supply nonempty id, report and actions")
        for check in (lambda: self.at.require_recorded("An order"),
                      lambda: self.first_call_deadline.require_recorded("A first-call deadline")):
            try:
                with MalformedRecord.refusing('order time'):
                    check()
            except MalformedRecord as error:
                problems.append(str(error))
        if problems:
            raise ValueError("\n".join(problems))

    NAMED_FILE: ClassVar[re.Pattern] = re.compile(r"([\w./-]+\.\w+):\d+")

    def require_transfer(self, todo: Todo) -> None:
        raise TodoError("Only an undelivered order can change owner.")

    def require_writable(self) -> None:
        for check in (self.check, *self.deletes.checks):
            check.require_writable()

    def delivery_rates(self) -> Iterator[tuple[str, float]]:
        """Undelivered orders provide no observed delivery duration."""
        return iter(())

    def untouched(self, touched: Sequence[str]) -> int:
        """The numbered edits naming a file:line where ``touched`` changed none of those files."""
        def changed(name: str) -> bool:
            return any(path.endswith(name.lstrip("/")) or name.endswith("/" + path)
                       for path in touched)
        return sum(1 for edit in self.edits
                   if (named := self.NAMED_FILE.findall(edit)) and not any(map(changed, named)))

    def copy_as[Kind: ProblemOrder](self, kind: type[Kind], **evidence) -> Kind:
        return kind(**{item.name: getattr(self, item.name) for item in fields(kind)
                       if item.name not in evidence}, **evidence)

    def merged(self, sha: str) -> MergedProblemOrder:
        return self.copy_as(MergedProblemOrder, sha=sha)

    def closed_by_merge(self, todo: Todo, registry: Registration,
                        pulls: PullRequests) -> tuple[Todo, ...]:
        repo = Repo(checkout=Path(self.check.directory), github=todo.repo)
        source = ProgressSource.of(todo, registry, pulls)
        return tuple(self.run.after_merge(todo, merged)
                     for merged in self.observed_merges(repo, source))

    def observed_merges(self, repo: Repo, source: ProgressSource) -> tuple[MergedProblemOrder, ...]:
        return tuple(self.merged(merged.sha) for merged in MergedProgress.found(source)
                     if repo.contains(merged.sha))

    def open_pull_request(self, todo: Todo, checkout: Path, title: str, body: str,
                          store: TodoStore, actor: ThreadIncarnation) -> str:
        """Create once; record the returned identity on this order, not in its prose."""
        todo.require_actor(actor, todo.held().generation)
        created = subprocess.run(
            ("gh", "pr", "create", "--draft", "-R", todo.repo.to_wire(),
             "--title", title, "--body-file", "-"),
            input=body,
            cwd=checkout,
            capture_output=True, text=True, timeout=60)
        if created.returncode:
            raise ValueError(created.stderr.strip() or "Order pull request creation failed.")
        url = created.stdout.strip()
        try:
            store.apply(todo.id, RecordPullRequest(todo.revision, actor,
                                                  PullRequestLink.created(todo.repo, url),
                                                  todo.held().generation))
        except (ValueError, TodoError, subprocess.SubprocessError) as error:
            raise ValueError(f"Created {url}, but recording its link failed: {error}. "
                             "Use order pr --existing; do not create another PR.") from error
        return url

    def work_instructions(self) -> tuple[str, ...]:
        return (f"Owner: {self.owner.to_wire()}",
                f"Check: {self.check.directory} :: {self.check.command}",
                *self.deletes.instructions(), *self.run.instructions(),
                *(f"{n}. {edit}" for n, edit in enumerate(self.edits, 1)),
                "Banned: " + "; ".join(self.banned), "Report: " + self.report)

    def instructions(self, todo):
        return "\n".join((f"Problem: {todo.id}: {todo.text}", *self.finding.instructions(),
            f"Order: {self.id}", *self.scope.instructions(todo.id),
            *self.work_instructions()))

    def delivered(self, delivery: CheckedDelivery):
        delivery.at.require_delivery()
        return self.copy_as(DeliveredProblemOrder, delivery=delivery)

    def installed(self, works: str, artifact: str) -> ProblemOrder:
        raise ValueError("Check the delivered order before recording installation.")

    def require_delivery(self, order_id: str) -> None:
        raise ValueError("Only an open order can be delivered.")

    def require_checked(self, problem: str) -> None:
        """An installed claim stands on this order's recorded check result."""
        raise ValueError(f"An installed claim needs the recorded check result of {problem}; "
                         "its order has none.")

    def withdrawn(self, previous: Assignment, why: str, superseded_by: Successor) -> ProblemOrder:
        self.require_withdrawal()
        return self.copy_as(WithdrawnProblemOrder, previous=previous, why=why,
                            superseded_by=superseded_by)


@dataclass(frozen=True, kw_only=True)
class AwaitingInstallationProblemOrder(CheckedProblemOrder, declared_name=None):
    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return (todo,)


@dataclass(frozen=True, kw_only=True)
class MergedProblemOrder(AwaitingInstallationProblemOrder):
    """The checked order installed by a merge, with its actual merge identity."""
    sha: str

    @property
    def line(self) -> str:
        return self.run.merged_line

    def closed_in(self, todo: Todo) -> Todo:
        return DoneTodoState.closed(todo, self)

    def waiting_in(self, todo: Todo) -> Todo:
        return (todo if todo.order == self
                else replace(todo, order=self, revision=todo.revision + 1))

    def observed_merges(self, repo: Repo, source: ProgressSource) -> tuple[MergedProblemOrder, ...]:
        return (self,)

    def require_close(self) -> None:
        self.run.require_merge_close()

    @property
    def installation_commits(self) -> tuple[str, ...]:
        return (self.sha,)

    def installed(self, works: str, artifact: str) -> ProblemOrder:
        self.run.require_closing(works)
        return self.copy_as(InstalledMergedProblemOrder, works=works, artifact=artifact)


@dataclass(frozen=True, kw_only=True)
class DeliverableProblemOrder(CheckedProblemOrder, declared_name=None):
    """Pending work and a holder's claim both await the commander's delivery verdict."""

    def corrected(self, correction: CorrectCheckTodo) -> DeliverableProblemOrder:
        if correction.previous != self.check:
            raise ValueError("The order's check changed since it was read; read it again.")
        return replace(self, check=correction.check,
                       check_corrections=(*self.check_corrections, correction))

    def require_delivery(self, order_id: str) -> None:
        if self.id != order_id:
            raise ValueError("The order changed; refresh its state before delivering it.")

    def sent_back(self, reason: StrikeReason) -> PendingProblemOrder:
        return self.fresh_order(time.time(), (*self.edits, reason.text))

    def fresh_order(self, at: float, edits: tuple[str, ...]) -> PendingProblemOrder:
        window = (self.first_call_deadline.recorded_at("A first-call deadline")
                  - self.at.recorded_at("An order"))
        return self.copy_as(
            PendingProblemOrder, id=uuid4().hex[:12], at=RecordedEventTime(at),
            first_call_deadline=RecordedEventTime(at + window), edits=edits,
        )


@dataclass(frozen=True, kw_only=True)
class PendingProblemOrder(DeliverableProblemOrder):
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    at: EventTime = field(default_factory=lambda: RecordedEventTime(time.time()))
    first_call_deadline: EventTime = field(
        default_factory=lambda: RecordedEventTime(time.time() + 120))

    def reissued(self, at: float) -> PendingProblemOrder:
        return self.fresh_order(at, self.edits)

    def claimed(self, claim: OrderClaim) -> ClaimedProblemOrder:
        return self.copy_as(ClaimedProblemOrder, claim=claim)

    pending: ClassVar[bool] = True
    line: ClassVar[str] = "Open"

    def require_transfer(self, todo: Todo) -> None:
        pass

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        """An undelivered order waits on install once its PR merged."""
        return read[todo.id].waiting_on_install(todo)

    def require_withdrawal(self) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class DroppedProblemOrder(UnorderedProblemOrder, CheckedProblemOrder):
    replacing_message: str
    line: ClassVar[str] = "Dropped for a newer order"


@dataclass(frozen=True)
class OrderClaim:
    """The holder's assertion, not the commander's verified delivery."""

    pull_request: PullRequestLink
    head: Annotated[str, GitCommitText]
    message: Message

    def __post_init__(self) -> None:
        GitCommitText.decode(self.head)
        if not self.message.body.strip():
            raise TodoError("A delivery claim needs its report text.")

    @classmethod
    def for_order(
        cls, todo: Todo, actor: ThreadIncarnation, number: int, head: str,
        report: str, answer: MessageReference,
    ) -> OrderClaim:
        return cls(
            PullRequestLink(todo.repo, number), head,
            Message(actor.name, todo.creator, report, InfoKind, answers=(answer,)),
        )


@dataclass(frozen=True, kw_only=True)
class ClaimedProblemOrder(DeliverableProblemOrder):
    claim: OrderClaim
    line: ClassVar[str] = "Claimed by the unit, waiting on the commander's verdict"

    def require_reassignment(self, todo: Todo, proposed: Assignment) -> None:
        """A corrected order to the claimant replaces its claim; another owner is a transfer."""
        if proposed.owner_identity != todo.holder_state.previous_assignment().owner_identity:
            self.require_transfer(todo)

    def observed_merges(self, repo: Repo, source: ProgressSource) -> tuple[MergedProblemOrder, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class DeliveredProblemOrder(AwaitingInstallationProblemOrder):
    delivery: OrderDelivery
    pending: ClassVar[bool] = False

    @property
    def line(self) -> str:
        return f"Delivered: {self.delivery.line}, waiting on install"

    def delivery_rates(self) -> Iterator[tuple[str, float]]:
        yield self.id, (
            self.delivery.at.recorded_at("A delivery") - self.at.recorded_at("An order")
        ) / len(self.edits)

    def story(self) -> tuple[StoryLine, ...]:
        return (*super().story(), *self.delivery.story())

    def require_transfer(self, todo: Todo) -> None:
        owner = todo.holder_state.previous_assignment().owner
        raise TodoError(f"{owner} already has {self.delivery.work}; {owner} merges it.")

    def delivered(self, delivery):
        if delivery != self.delivery:
            raise ValueError("The order already records a different delivery.")
        return self

    def observed_merges(self, repo: Repo, source: ProgressSource) -> tuple[MergedProblemOrder, ...]:
        commits = self.delivery.merged_commits(repo)
        return (tuple(self.merged(commit) for commit in commits)
                or super().observed_merges(repo, source))

    def closing_artifact(self) -> str:
        return self.delivery.closing_artifact()

    @property
    def installation_commits(self) -> tuple[str, ...]:
        return self.delivery.installation_commits

    @property
    def installation_build(self) -> BuildIdentity:
        return self.delivery.installation_build

    def installed(self, works: str, artifact: str) -> InstalledProblemOrder:
        self.run.require_closing(works)
        return self.copy_as(InstalledProblemOrder, works=works, artifact=artifact)

    def require_checked(self, problem: str) -> None:
        """The delivery passed the order's check, so its result is recorded."""


@dataclass(frozen=True, kw_only=True)
class ReportedProblemOrder(DeliveredProblemOrder):
    """A triage answer is complete when returned, with no fictitious installation."""

    delivery: ReportDelivery
    line: ClassVar[str] = "Report returned"

    def require_close(self) -> None:
        pass

    def observed_merges(self, repo: Repo, source: ProgressSource) -> tuple[MergedProblemOrder, ...]:
        return ()

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class InstalledEvidence(CheckedProblemOrder, declared_name=None):
    """Recorded application evidence, shared by delivered and merge-observed orders."""
    works: str
    artifact: str
    line: ClassVar[str] = "Installed"

    @property
    def accepted(self) -> bool:
        return True

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.works.strip() or not self.artifact.strip():
            raise ValueError("Describe what worked and name its artifact file.")

    def installed(self, works: str, artifact: str) -> Self:
        if (works, artifact) != (self.works, self.artifact):
            raise ValueError("The order already records different installed evidence.")
        return self

    def require_close(self) -> None:
        pass

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class InstalledProblemOrder(InstalledEvidence, DeliveredProblemOrder):
    def merged(self, sha: str) -> MergedProblemOrder:
        return self.copy_as(InstalledMergedProblemOrder, sha=sha)


@dataclass(frozen=True, kw_only=True)
class InstalledMergedProblemOrder(InstalledEvidence, MergedProblemOrder):
    """The installed merge and the recorded run share the same evidence behavior."""


@dataclass(frozen=True)
class MeasuredReview:
    """When the commander first reviews an order, from a snapshot's distinct delivered-order
    observations; never another duration store, never a due time shown to the unit."""

    rates: tuple[float, ...]

    @classmethod
    def read(cls, store: TodoStore) -> MeasuredReview:
        rates = {
            identity: rate
            for todo in store._select("1", ())
            for order in todo.order_history
            for identity, rate in order.delivery_rates()
        }
        return cls(tuple(rates.values()))

    @cached_property
    def seconds_per_edit(self) -> float:
        return median(self.rates)

    @property
    def summary(self) -> str:
        if not self.rates:
            return "Measured median: unavailable (0 delivered orders); minimum 2 minutes"
        return (f"Measured median: {self.seconds_per_edit:g} seconds per edit "
                f"from {len(self.rates)} delivered orders")

    def review_at(self, order: CheckedProblemOrder) -> RecordedEventTime:
        seconds = self.seconds_per_edit * len(order.edits) if self.rates else 0
        at = order.at.recorded_at("An order")
        return RecordedEventTime(ceil((at + max(120, seconds)) / 60) * 60)


@dataclass(frozen=True, kw_only=True)
class WithdrawnProblemOrder(CheckedProblemOrder):
    """An order the commander sent in error and retired before delivery; it strikes no one."""

    previous: Assignment
    why: str
    superseded_by: Annotated[Successor, WireOmitDefault()] = NoSuccessor()
    pending: ClassVar[bool] = False
    line: ClassVar[str] = "Withdrawn"

    def require_close(self) -> None:
        """Retiring an erroneous order does not claim installed work."""

    def withdrawals(self, todo: Todo) -> tuple[str, ...]:
        return (f"{todo.id}: order {self.id} withdrawn from {self.previous.owner}, "
                f"no strike: {self.why}{self.superseded_by.withdrawal_note()}",)


@dataclass(frozen=True)
class Hunt(DeclaredFamily, affix="Hunt"):
    where: FileLine
    why: str
    at: EventTime

    def __post_init__(self):
        if len(self.why.split()) < 10:
            raise ValueError("Describe the inspected code and verdict in ten or more words.")
        self.at.require_recorded("A hunt")

    def require_commander(self, store: TodoStore, commander: ThreadIncarnation) -> None:
        """The commander boundary validates source references before recording a hunt."""


@dataclass(frozen=True)
class CleanHunt(Hunt):
    pass


@dataclass(frozen=True)
class OrderedHunt(Hunt):
    sent: str

    def require_commander(self, store: TodoStore, commander: ThreadIncarnation) -> None:
        super().require_commander(store, commander)
        if not any(todo.id == self.sent
                   for todo in store.held_for_commander(commander)):
            raise ValueError("The hunt must name this commander's current order.")


@dataclass(frozen=True)
class HuntRecord(TypedTable):
    id: Annotated[str, Column(primary_key=True)]
    commander: Annotated[ThreadIncarnation, Column(index=True)]
    hunt: Hunt


class TodoError(ToolRefusedError):
    """A todo request is invalid or its persisted state cannot be trusted."""


class TodoStoreUnwritable(TodoError):  # noqa: N818 - domain-specific storage failure
    """The todo store could not complete its write; its original cause is preserved."""


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


class TodoHolder(ABC):
    """Current, released and absent holders own their assignment behaviour."""

    def held(self, current: Todo) -> Assignment:
        raise TodoError(f"{current.id} has no open order.")

    @abstractmethod
    def previous_assignment(self) -> Assignment: ...

    def owned_by(self, actor: ThreadIncarnation, generation: str) -> bool:
        return False

    def proposed_assignment(self, proposed: Assignment) -> Assignment:
        return proposed

    @abstractmethod
    def require_trace(self, owner: str, traced: SourceReference) -> None: ...

    def successor(self, proposed: Assignment, current: Todo) -> Assignment:
        return proposed

    def current_assignments(self) -> tuple[Assignment, ...]:
        return ()


class NoTodoHolder(TodoHolder):
    def previous_assignment(self) -> Assignment:
        raise TodoError("Problem has no previous holder.")

    def require_trace(self, owner: str, traced: SourceReference) -> None:
        pass


@dataclass(frozen=True, slots=True)
class Assignment(TodoHolder):
    owner: str
    owner_created: float
    parent: str
    parent_created: float
    generation: str
    previous_owners: Annotated[
        tuple[ThreadIncarnation, ...], WireOmitDefault()
    ] = ()

    @classmethod
    def of(cls, owner: ThreadIncarnation, parent: ThreadIncarnation, generation: str) -> Assignment:
        return cls(owner.name, owner.wire_birth, parent.name, parent.wire_birth, generation)

    def __post_init__(self) -> None:
        _text(self.owner, "Thread name", 128)
        _text(self.parent, "Thread name", 128)
        try:
            self.owner_identity.require_recorded()
        except ValueError as error:
            raise TodoError("Assignment requires a recorded owner incarnation") from error
        _text(self.generation, "Assignment generation", 128)

    @property
    def owner_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created)

    @property
    def parent_identity(self) -> ThreadIncarnation:
        """Who made the assignment: the order's commander, or Core's watcher."""
        return ThreadIncarnation.from_wire({"name": self.parent, "created_at": self.parent_created})

    def held(self, current: Todo) -> Assignment:
        return self

    def proposed_assignment(self, proposed: Assignment) -> Assignment:
        return replace(proposed, previous_owners=self.previous_owners)

    def current_assignments(self) -> tuple[Assignment, ...]:
        return (self,)

    def owned_by(self, actor: ThreadIncarnation, generation: str) -> bool:
        return (self.owner_identity, self.generation) == (actor, generation)

    def require_trace(self, owner: str, traced: SourceReference) -> None:
        owners = {self.owner, *(previous.name for previous in self.previous_owners)}
        if owner != self.owner and len(owners) >= 2:
            traced.require(f"This problem has had {sorted(owners)}; read the failing path "
                           "and pass --traced PATH:LINE.")

    def previous_assignment(self) -> Assignment:
        """The current holder supplies its own assignment for retirement."""
        return self

    def successor(self, proposed: Assignment, current: Todo) -> Assignment:
        if self.generation == proposed.generation and self.owner_identity != proposed.owner_identity:
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
    def assign(cls, current: Todo, proposed: Assignment, traced: SourceReference) -> Todo:
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
    def assign(cls, current, proposed, traced):
        return current.assigned(proposed, traced)


class BlockedTodoState(TodoState):
    pass


class DoneTodoState(TodoState):
    open: ClassVar[bool] = False

    @classmethod
    def require_open(cls, current: Todo) -> None:
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
        return cls.closed(current, current.order)

    @classmethod
    def closed(cls, current: Todo, order: ProblemOrder) -> Todo:
        order.require_close()
        return replace(current, state=cls, order=order, assignment=None,
                       revision=current.revision + 1)


@dataclass(frozen=True)
class TodoCommand(Command, DeclaredFamily, affix="TodoOperation"):
    expected_revision: int

    def __post_init__(self) -> None:
        if FieldCodec.decode(int, self.expected_revision) <= 0:
            raise TodoError("Todo operation requires a positive exact revision")

    def prepare(self, goals: Goals, current: Todo) -> Self:
        return self

    def record(self, goals: Goals, problem: str) -> Todo:
        """Write this operation; publish its effects only after the canonical row was written."""
        changed = goals.apply_problem(problem, self)
        self.after_record(goals, changed)
        return changed

    def after_record(self, goals: Goals, changed: Todo) -> None:
        """Most operations have no automatic public effect."""

    def require_unit(self, db: sqlite3.Connection, current: Todo) -> None:
        """Only assignment operations compete for a unit."""

    @abstractmethod
    def apply(self, current: Todo) -> Todo: ...


class TodoOperation(TodoCommand, declared_name=None):
    """Commands the ordinary commander's tools accept."""


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    """The order's commander gives the problem to a unit."""

    proposed: Assignment
    review_at: EventTime
    order: PendingProblemOrder | MergedProblemOrder
    goal: GoalReference
    traced: Annotated[FileLine | NoSourceReference, WireOmitDefault()] = NoSourceReference()

    def require_unit(self, db: sqlite3.Connection, current: Todo) -> None:
        TodoStore.require_unit_in(db, self.proposed.owner_identity, current.creator_identity)

    def apply(self, current: Todo) -> Todo:
        self.review_at.require_recorded("A review time")
        proposed = current.proposed_assignment(self.proposed)
        if (current.assignment, current.review_at, current.order, current.goal) == (
                proposed, self.review_at, self.order, self.goal):
            return current
        current.require_revision(self.expected_revision)
        self.actor.require_may_change(current)
        if self.order != current.order:
            self.order.require_wording()
            self.order.require_writable()
        assigned = (current if current.assignment == proposed
                    else current.state.assign(current, proposed, self.traced))
        return replace(assigned, review_at=self.review_at,
                       order=self.order, prior_orders=current.orders_before(self.order),
                       goal=self.goal,
                       revision=current.revision + 1)

    @property
    def actor(self) -> ThreadIncarnation:
        return self.proposed.parent_identity


@dataclass(frozen=True)
class CitedStrike(TodoOperation, declared_name=None):
    evidence: Annotated[tuple[FileLine, ...], WireOmitDefault()] = field(
        default=(), kw_only=True)

    def prepare(self, goals: Goals, current: Todo) -> Self:
        if not self.evidence:
            raise ValueError("A strike needs a checkable file:line, not a board row or goal text.")
        order = current.order.checked()
        root, revision = Path(order.check.directory), order.cited_revision
        for cited in self.evidence:
            cited.require_exists(root, revision)
        return self


@dataclass(frozen=True)
class StrikeTodo(CitedStrike, StoryEvent):
    actor: ThreadIncarnation
    previous: Assignment
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        self.actor.require_may_change(current)
        current.require_assignment(self.previous)
        _text(self.reason, "Strike reason", 2000)
        StrikeReason(self.reason)
        if any(self.repeats(recorded) for recorded in self.prior_strikes(current)):
            return current
        return replace(current, strikes=(*current.strikes, self), revision=current.revision + 1)

    def story_text(self) -> str:
        return f"{self.actor.name} struck {self.previous.owner}: {self.reason}"

    def prior_strikes(self, current: Todo) -> tuple[StrikeTodo, ...]:
        return current.live_strikes(self.previous.owner)

    def matches_failed_check(self, strike: FailedCheckStrike) -> bool:
        return False

    def repeats(self, recorded: StrikeTodo) -> bool:
        return False


@dataclass(frozen=True, kw_only=True)
class FailedCheckStrike(StrikeTodo):
    """DeliverTodo records a failed order check at the check boundary, before rejecting done;
    once per order and holder, in the existing strike history."""

    order_id: str

    @classmethod
    def make(cls, todo: Todo, now: float, reason: str) -> Self:
        return cls(todo.revision, CoreWatcherIncarnation.watcher(), todo.held(), reason,
                   RecordedEventTime(now), order_id=todo.order.checked().id,
                   evidence=(todo.order.checked().owner,))

    def prior_strikes(self, current: Todo) -> tuple[StrikeTodo, ...]:
        # A void is not permission to repeat this same automatic failure on every pass.
        return current.strikes

    def matches_failed_check(self, strike: FailedCheckStrike) -> bool:
        return (self.order_id == strike.order_id
                and self.previous.owner_identity == strike.previous.owner_identity)

    def repeats(self, recorded: StrikeTodo) -> bool:
        return recorded.matches_failed_check(self)

    def apply(self, current: Todo) -> Todo:
        struck = super().apply(current)
        if struck != current and struck.struck_out(self.previous.owner):
            return ReleaseTodo(struck.revision, self.actor, self.previous,
                               StrikeReason(self.reason), self.at).apply(struck)
        return struck

    def after_record(self, goals: Goals, changed: Todo) -> None:
        if changed.revision == self.expected_revision:
            return
        count = len(changed.live_strikes(self.previous.owner))
        line = (f"Core strike {count} on {changed.id} order {self.order_id}: "
                f"{self.previous.owner} {self.reason}"
                + (f"; {changed.id} taken away" if not changed.holder else ""))
        goals.bus.publisher.publish_ordinary(
            Message(sender=changed.creator, target="#comms", body=line, type=InfoKind),
            origin=AgentOrigin())


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
class OrderVerdict(TodoCommand, StoryEvent, declared_name=None):
    """The commander's verdict on one tool call the holder made for its order, kept on the order."""
    audience: ClassVar[type[VerdictAudience]] = VerdictAudience
    actor: ThreadIncarnation
    previous: Assignment
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()), kw_only=True)

    def story_text(self) -> str:
        return f"{self.actor.name} judged {self.previous.owner}: {self.line}"

    def implied_strikes(self) -> tuple[StrikeTodo, ...]:
        return ()

    def judged_calls(self) -> tuple[str, ...]:
        """The holder's tool calls this verdict judges by identity."""
        return ()

    def judged_through(self, since: float) -> float:
        """The instant before which no edit is left unjudged once this verdict is recorded."""
        return since

    @abstractmethod
    def judged(self, current: Todo) -> Todo: ...

    @property
    @abstractmethod
    def line(self) -> str: ...

    def apply(self, current: Todo) -> Todo:
        # A verdict's time bounds which edits it judges, so the write refuses one it cannot date.
        self.at.require_recorded("A verdict")
        current.require_revision(self.expected_revision)
        current.require_assignment(self.previous)
        judged = self.judged(current)
        return replace(judged, verdicts=(*judged.verdicts, self), revision=current.revision + 1)


@dataclass(frozen=True)
class ToolCallVerdict(OrderVerdict, TodoOperation, declared_name=None):
    tool_call_id: str

    def judged_calls(self) -> tuple[str, ...]:
        return (self.tool_call_id,)

    def story_text(self) -> str:
        return f"{super().story_text()}; tool call {self.tool_call_id}"

    def apply(self, current: Todo) -> Todo:
        current.require_commander(self.actor)
        _text(self.tool_call_id, "Judged tool call ID", 200)
        return super().apply(current)


@dataclass(frozen=True)
class QuotedVerdict(OrderVerdict, declared_name=None):
    quote: str

    def story_text(self) -> str:
        return f'{super().story_text()}; quoted: "{self.quote}"'

    def require_quote(self, text: str) -> None:
        quoted = " ".join(self.quote.split())
        if len(quoted.split()) < 4 or quoted not in " ".join(text.split()):
            raise ValueError("Quote 4+ words of the order this verdict carries out.")

    def judged(self, current: Todo) -> Todo:
        self.require_quote(current.order.instructions(current))
        return current


@dataclass(frozen=True)
class AcceptedVerdict(QuotedVerdict, ToolCallVerdict):
    """The tool call carries out the order clause it quotes."""
    line = "Accepted"

    @classmethod
    def require_branches(cls, added: int, no_owner: str | None) -> None:
        if added <= 0:
            return
        if not (no_owner and len(no_owner.split()) >= 12 and re.search(r'\bbecause\b', no_owner)
                and re.search(r'/\S+:\d+', no_owner)):
            raise ValueError('Added branches need --no-owner: 12+ words with because and an absolute file:line.')


@dataclass(frozen=True)
class ReviewedHeadVerdict(QuotedVerdict):
    """The connector reviewer accepts this immutable commit, never an inferred checkout head."""
    context: ClassVar[str] = "agent-comms/review"
    head: str

    def __post_init__(self) -> None:
        super().__post_init__()
        GitCommitText.decode(self.head)

    @property
    def line(self) -> str:
        return f"Reviewed head {self.head}"

    def judged_through(self, since: float) -> float:
        """Accepting a head judges the whole diff, so every edit made before it is judged."""
        return self.at.latest_with(since)

    def reviewed(self, goals: Goals, problem: str, reviewer: ThreadIncarnation) -> Todo:
        if self.actor != reviewer:
            raise TodoError("The verdict actor must be the connector reviewer recording it.")
        current = goals.todos.get(problem)
        recorded = self.prepare(goals, current).record(goals, problem)
        posted = subprocess.run(
            ("gh", "api", "--method", "POST", f"repos/{recorded.repo.to_wire()}/statuses/{self.head}",
             "-f", "state=success", "-f", f"context={self.context}",
             "-f", f"description=Accepted by {reviewer.name} on {problem}"),
            capture_output=True, text=True, timeout=60,
        )
        if posted.returncode:
            raise TodoError(posted.stderr.strip() or f"Review status on {self.head} failed.")
        return recorded


@dataclass(frozen=True)
class PeerAcceptedVerdict(AcceptedVerdict):
    peer: str

    def prepare(self, goals, current):
        with goals.bus.log.opened_wire_snapshot() as opened, goals.bus.log.checked_read() as source:
            message = PublicMessages.message_by_id(source, opened, self.peer)
        if message is None:
            raise ValueError("The peer order message does not exist.")
        if (message.sender == self.actor.name or
                (message.target != current.holder and
                 current.holder not in {mention.name for mention in message.mentions})):
            raise ValueError("--peer must name another sender's message to this unit.")
        self.require_quote(message.body)
        return self

    def judged(self, current: Todo) -> Todo:
        # prepare checked the quote against its retained peer message, not our order.
        return current


@dataclass(frozen=True)
class SupersededVerdict(ToolCallVerdict):
    replacement: str
    reason: str
    line = "Superseded"

    def story_text(self) -> str:
        return f"{super().story_text()} by {self.replacement}: {self.reason}"

    def prepare(self, goals, current):
        replacing = [todo for todo in goals.todos.held_for_commander(self.actor)
                     if todo.id == self.replacement]
        if not replacing:
            raise ValueError('The replacing problem is not held for this commander.')
        if any(todo.holder == current.holder for todo in replacing):
            raise ValueError('The replacing order went to the same unit; judge its edits ok or back.')
        if len(self.reason.split()) < 6:
            raise ValueError('Say why this edit was replaced in 6+ words.')
        return self

    def judged(self, current: Todo) -> Todo:
        return current


@dataclass(frozen=True)
class DroppedVerdict(ToolCallVerdict):
    """The unit discarded the edit: no branch, remote branch or worktree of its repository holds it.

    Git answers this at judge time; the commander supplies only the edit Core recorded.
    """
    path: str
    added: tuple[str, ...]
    line = "Dropped: on no branch, remote branch or worktree"

    def prepare(self, goals: Goals, current: Todo) -> Self:
        lines = tuple(text.strip() for text in self.added if len(text.strip()) >= 4)
        if not lines:
            raise TodoError(
                "An edit that adds no line cannot be shown dropped; judge it ok or back.")
        self.refuse_held(Path(current.order.checked().check.directory), lines)
        return self

    def refuse_held(self, checkout: Path, lines: tuple[str, ...]) -> None:
        """Refuse while any place still holds every added line: the file on disk, then any
        branch, remote branch or worktree HEAD of the checkout."""
        target = Path(self.path)
        if target.is_file() and all(line in target.read_text(errors="replace") for line in lines):
            self.refuse(str(target))
        refs = self.git(checkout, "for-each-ref", "--format=%(objectname)",
                        "refs/heads", "refs/remotes")
        heads = [entry.removeprefix("HEAD ")
                 for entry in self.git(checkout, "worktree", "list", "--porcelain")
                 if entry.startswith("HEAD ")]
        patterns = [arg for line in lines for arg in ("-e", line)]
        found = self.git(checkout, "grep", "-F", "-l", "--all-match", *patterns,
                         *sorted({*refs, *heads}), "--", f":(glob)**/{target.name}",
                         allowed=(0, 1))
        if found:
            self.refuse(found[0].split(":", 1)[0])

    def refuse(self, holder: str) -> None:
        raise TodoError(f"Edit {self.tool_call_id} is still on {holder}; judge it ok or back.")

    @staticmethod
    def git(checkout: Path, *args: str, allowed: tuple[int, ...] = (0,)) -> list[str]:
        ran = subprocess.run(("git", "-C", str(checkout), *args),
                             capture_output=True, text=True, timeout=120)
        if ran.returncode not in allowed:
            raise TodoError(ran.stderr.strip() or f"git {args[0]} failed in {checkout}.")
        return ran.stdout.splitlines()

    def judged(self, current: Todo) -> Todo:
        return current


@dataclass(frozen=True)
class SendBackVerdict(ToolCallVerdict, CitedStrike):
    """The tool call goes back; the same write strikes the holder and revises the order Goal."""
    reason: StrikeReason

    @property
    def line(self) -> str:
        return f"Sent back: {self.reason.text}"

    def implied_strikes(self) -> tuple[StrikeTodo, ...]:
        return (StrikeTodo(self.expected_revision, self.actor, self.previous, self.reason.text,
                           self.at, evidence=self.evidence),)

    def story_text(self) -> str:
        return f"{super().story_text()} (strike recorded)"

    def judged(self, current: Todo) -> Todo:
        (strike,) = self.implied_strikes()
        struck = strike.apply(current)
        order = current.order.checked().sent_back(self.reason)
        return replace(struck, order=order, prior_orders=current.orders_before(order))

    def record(self, goals: Goals, problem: str) -> Todo:
        return goals.send_back(problem, self)


@dataclass(frozen=True)
class VoidStrikeTodo(TodoOperation, StoryEvent):
    """The commander takes back a strike recorded in error; the strike stays on the record."""
    actor: ThreadIncarnation
    strike: StrikeTodo
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def story_text(self) -> str:
        return (f"{self.actor.name} voided a strike against {self.strike.previous.owner}: "
                f"{self.reason}")

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        if self.strike not in current.live_strikes(self.strike.previous.owner):
            raise TodoConflict(current)
        _text(self.reason, "Void reason", 2000)
        StrikeReason(self.reason)
        return replace(current, voided=(*current.voided, self), revision=current.revision + 1)


@dataclass(frozen=True)
class ReleaseTodo(TodoOperation, StoryEvent, TodoHolder):
    """The commander takes an open problem off its unit; the order and its strikes stay."""

    def previous_assignment(self) -> Assignment:
        """The release keeps the assignment of the holder it removed."""
        return self.previous

    def proposed_assignment(self, proposed: Assignment) -> Assignment:
        return self.previous.handed_to(proposed)

    def require_trace(self, owner: str, traced: SourceReference) -> None:
        self.previous.require_trace(owner, traced)

    def story_text(self) -> str:
        return f"{self.actor.name} released {self.previous.owner}: {self.reason.text}"
    actor: ThreadIncarnation
    previous: Assignment
    reason: StrikeReason
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        self.actor.require_may_change(current)
        current.require_assignment(self.previous)
        return replace(current, assignment=None, released=(*current.released, self),
                       revision=current.revision + 1)


@dataclass(frozen=True)
class CorrectCheckTodo(TodoOperation):
    """The commander replaces a wrong check of an open order.

    The order keeps its id, holder, review timer and strikes; a correction is not a resend."""
    actor: ThreadIncarnation
    previous: OrderCheck
    check: OrderCheck
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.state.require_open(current)
        _text(self.reason, "Check correction reason", 2000)
        self.check.require_writable()
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
class RecordPullRequest(TodoCommand):
    actor: ThreadIncarnation
    link: PullRequestLink
    generation: str

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_actor(self.actor, self.generation)
        if self.link.repo != current.repo:
            raise TodoError("A recorded pull request must belong to the todo's repository.")
        order = current.order.checked()
        if self.link in order.pull_requests:
            return current
        return replace(current, revision=current.revision + 1,
                       order=replace(order, pull_requests=(*order.pull_requests, self.link)))


@dataclass(frozen=True)
class ServeTodo(TodoOperation):
    """The commander names the plan phase an open order serves; it keeps its id and holder."""
    actor: ThreadIncarnation
    serves: type[OrderPhase]

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(current, order=current.order.for_phase(self.serves),
                       revision=current.revision + 1)


@dataclass(frozen=True)
class ClaimOrder(TodoCommand):
    """Only the holder records a claim; its answer is published from the retained value."""

    actor: ThreadIncarnation
    generation: str
    claim: OrderClaim

    def apply(self, current: Todo) -> Todo:
        current.require_revision(self.expected_revision)
        current.state.require_open(current)
        current.require_assignee(self.actor, self.generation)
        order = current.order.checked().claimed(self.claim)
        return replace(current, order=order, revision=current.revision + 1)

    def after_record(self, goals: Goals, changed: Todo) -> None:
        goals.bus.publisher.publish_ordinary(self.claim.message, origin=AgentOrigin())


@dataclass(frozen=True)
class DeliverTodo(TodoOperation, declared_name=None):
    """Internal delivery command; the order-done tool owns its wire boundary and claims."""

    actor: ThreadIncarnation
    delivery: OrderDelivery
    order_id: str
    verified_claims: Annotated[dict[ProgressClaim, tuple[str, ...]], WireTransient()] = field(
        default_factory=dict, kw_only=True, repr=False, compare=False,
    )

    def prepare(self, goals, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.state.require_open(current)
        current.order.require_delivery(self.order_id)
        verified = self.delivery.verified(current, goals.registry, self.verified_claims)
        try:
            delivered = asyncio.run(verified)
        except OrderCheckFailedError as error:
            self.delivery.check_failed(goals, current, error)
            raise
        return replace(self, delivery=delivered)

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.state.require_open(current)
        current.order.require_delivery(self.order_id)
        self.delivery.at.require_delivery()
        return current.order.checked().scope.completed(current, self.delivery)


@dataclass(frozen=True)
class CloseOrderTodo(TodoOperation, declared_name=None):
    """The commander records the order's terminal state and closes its problem in one revision."""
    actor: ThreadIncarnation

    @abstractmethod
    def closed(self, current: Todo) -> ProblemOrder: ...

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return DoneTodoState.closed(current, self.closed(current))


@dataclass(frozen=True)
class InstallTodo(CloseOrderTodo):
    works: str
    artifact: str

    def prepare(self, goals: Goals, current: Todo) -> InstallTodo:
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.order.require_installation()
        return self

    def closed(self, current: Todo) -> ProblemOrder:
        if not Path(self.artifact).is_file():
            raise ValueError("Retain the installed application artifact before closing.")
        return current.order.installed(self.works, self.artifact)


@dataclass(frozen=True)
class WithdrawTodo(CloseOrderTodo):
    """An undelivered order sent in error leaves its problem with its reason and no strike."""
    previous: Assignment
    why: str
    superseded_by: Successor = NoSuccessor()

    def closed(self, current: Todo) -> ProblemOrder:
        order = current.order.withdrawn(self.previous, self.why, self.superseded_by)
        if current.holder_state.previous_assignment() != self.previous:
            raise TodoConflict(current)
        _text(self.why, "Withdrawal reason", 2000)
        return order


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
    unit_index: ClassVar[ExpressionIndex] = ExpressionIndex((
        "json_extract({assignment}, '$.owner')",
        "json_extract({assignment}, '$.owner_created')", "{state}",
    ))
    indexes = (Index(("creator", "creator_created", "state")), Index(("state",)), unit_index)

    id: Annotated[str, Column(primary_key=True)]
    repo: Annotated[RepositoryName, Column(storage=TextStorage)]
    text: str
    creator: str
    creator_created: float
    state: type[TodoState]
    revision: Annotated[int, Column(check="revision>0")]
    goal: Annotated[GoalReference, Column(storage=JsonStorage, nullable=True)]
    assignment: Assignment | None
    order: ProblemOrder
    review_at: EventTime = field(default_factory=UnrecordedEventTime)
    """When the commander next reviews the held order; the unit is never shown it."""
    strikes: tuple[StrikeTodo, ...] = ()
    voided: tuple[VoidStrikeTodo, ...] = ()
    released: tuple[ReleaseTodo, ...] = ()
    verdicts: tuple[OrderVerdict, ...] = ()
    corrections: tuple[WordingCorrection, ...] = ()
    messages: tuple[OrderMessage, ...] = ()
    prior_orders: tuple[CheckedProblemOrder, ...] = ()

    @property
    def judged_calls(self) -> tuple[str, ...]:
        return tuple(call for verdict in self.verdicts for call in verdict.judged_calls())

    @property
    def unjudged_since(self) -> float:
        """Edits before the order, or before a later verdict judging a whole head, are judged."""
        since = self.order.checked().at.recorded_at("An order")
        for verdict in self.verdicts:
            since = verdict.judged_through(since)
        return since

    @property
    def order_history(self) -> tuple[CheckedProblemOrder, ...]:
        return (*self.prior_orders, *self.order.declarations)

    def orders_before(
        self, following: ProblemOrder, additional: tuple[CheckedProblemOrder, ...] = (),
    ) -> tuple[CheckedProblemOrder, ...]:
        current = {order.id for order in following.declarations}
        return tuple({order.id: order for order in (*self.order_history, *additional)
                      if order.id not in current}.values())

    def recurrence(
        self, locations: tuple[FileLine, ...], root: Path, now: float,
    ) -> tuple[str, ...]:
        recent = tuple(order for order in self.order_history
                       if order.at.since(now - 7 * 86400)
                       and order.at.recorded_at("An earlier order") <= now
                       and order.on_files(locations, root))
        if not recent:
            return ()
        previous = max(recent, key=lambda order: order.at.chronology())
        age = now - previous.at.recorded_at("An earlier order")
        finding = ''.join(f'; {line}' for line in previous.finding.instructions())
        return (f'{self.id}: {previous.owner.to_wire()} ({age:.0f}s old){finding}',)

    @property
    def held_order_ids(self) -> tuple[str, ...]:
        return tuple(order.id for order in self.order_history)

    @property
    def reference_pattern(self) -> str:
        return rf"(^|[^A-Za-z0-9_]){re.escape(self.id)}([^A-Za-z0-9_]|$)"

    unrequested_goal: ClassVar[GoalReference] = NoGoalReference()

    @classmethod
    def ordered(cls, todo_id: str, repo: RepositoryName, text: str, creator: ThreadIncarnation,
                assignment: Assignment, review_at: EventTime,
                order: PendingProblemOrder | MergedProblemOrder,
                goal: GoalReference = unrequested_goal) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 2000)
        review_at.require_recorded("A review time")
        goal.require_reference()
        return cls(todo_id, repo, text, creator.name, creator.wire_birth, OpenTodoState,
                   1, goal, assignment, order, review_at)

    @property
    def pending(self):
        return self.order.pending

    def story(self) -> ProblemStory:
        implied = frozenset(strike for verdict in self.verdicts
                            for strike in verdict.implied_strikes())
        sources: tuple[StorySource, ...] = (
            self.order, *(strike for strike in self.strikes if strike not in implied),
            *self.voided, *self.released, *self.verdicts,
        )
        events = tuple(sorted((event for source in sources for event in source.story()),
                              key=lambda event: event.at.chronology()))
        return ProblemStory(self.id, self.order.line, events)

    @property
    def review_window(self) -> float:
        """The measured time from the order to its first review; each later review is one more."""
        return (self.review_at.recorded_at("A review time")
                - self.order.checked().at.recorded_at("An order"))

    def timer_up(self, now: float) -> tuple[RecordedEventTime, ...]:
        """The review round an undelivered order is in at ``now``. Like an item timer it comes
        back every window until the order is delivered; no round strikes or moves anyone."""
        first = self.review_at.recorded_at("A review time")
        if not self.pending or now < first:
            return ()
        rounds = (now - first) // self.review_window
        return (RecordedEventTime(first + rounds * self.review_window),)

    @property
    def handed_out_on_release(self) -> bool:
        """Whoever released this row last says whether an idle unit gets it unasked."""
        return any(release.actor.hands_out_released_work for release in self.released[-1:])

    def reissued_review(self, at: float) -> EventTime:
        """A reissued order keeps its measured window from its reissue time."""
        return RecordedEventTime(at + self.review_window)

    @property
    def creator_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.creator, self.creator_created)

    @property
    def holder(self) -> str | None:
        """The unit holding this problem; None once it is closed or released."""
        return next((held.owner for held in self.holder_state.current_assignments()), None)

    @property
    @boundary_decoder
    def holder_state(self) -> TodoHolder:
        """Decode the stored optional assignment once into its behaviour owner."""
        if self.assignment is not None:
            return self.assignment
        return self.released[-1] if self.released else NoTodoHolder()

    def held(self) -> Assignment:
        """The open order's assignment; a problem nobody holds has no order work."""
        return self.holder_state.held(self)

    @property
    def strike_count(self) -> int:
        return len(self.live_strikes(self.holder))

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

    @boundary_decoder
    def require_assignee(self, actor: ThreadIncarnation, generation: str | None) -> None:
        if generation is None or not self.holder_state.owned_by(actor, generation):
            raise TodoConflict(self)

    def proposed_assignment(self, proposed: Assignment) -> Assignment:
        return self.holder_state.proposed_assignment(proposed)

    def assigned(self, proposed: Assignment, traced: SourceReference) -> Todo:
        self.order.require_reassignment(self, proposed)
        holder = self.holder_state
        holder.require_trace(proposed.owner, traced)
        proposed = holder.successor(proposed, self)
        return replace(self, assignment=proposed, revision=self.revision + 1)

    def require_released_order(self, owner: ThreadIncarnation, goal_id: str) -> None:
        if self.holder_state.owned_by(owner, goal_id):
            raise TodoConflict(self)

    @property
    def latest_message(self) -> OrderMessage:
        if not self.messages:
            raise ValueError(f"{self.holder} holds {self.id}, but no order message was recorded.")
        return self.messages[-1]

    def require_fold(self, target: Todo, why: str) -> None:
        self.state.require_open(self)
        target.state.require_open(target)
        if self.id == target.id:
            raise ValueError("Fold needs two different open problems.")
        if len(why.split()) < 8:
            raise ValueError("Say why they are the same problem in 8+ words.")
        self.order.folded(target.id, why)

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
                review_at=proposed.review_at,
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


@dataclass(frozen=True)
class ProblemNumber(TypedTable):
    """Allocated global problem numbers, including reservations not yet sent."""

    number: Annotated[int, Column(primary_key=True, auto_increment=True)]

    @classmethod
    def reserve(cls, db: sqlite3.Connection, problem: str) -> None:
        if re.fullmatch(r'P[1-9]\d*', problem):
            cls(int(problem[1:])).insert_if_absent(db)

    @classmethod
    def allocate(cls, db: sqlite3.Connection) -> str:
        cursor = db.execute(f'INSERT INTO "{cls.declared_name}" DEFAULT VALUES')
        return f'P{cursor.lastrowid}'


@dataclass(frozen=True, slots=True)
class TodoStore(DurableStore):
    """One SQLite row owns both todo state and its exclusive assignment.

    Assignment identity and revision deduplicate known operations. An uncertain
    input is not permission to replay it; inspect its original retained result.
    This store never launches a child.
    """

    root: Path
    filename: ClassVar[str] = "todos.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (Todo, HuntRecord, ProblemNumber)

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        try:
            with super(TodoStore, self).writing() as connection:
                yield connection
        except sqlite3.Error as error:
            raise TodoStoreUnwritable(
                f"{self.path}: {type(error).__name__}: {error}"
            ) from error

    @contextmanager
    def writer_connection(
        self, *, check_same_thread: bool = True
    ) -> Iterator[sqlite3.Connection]:
        with (
            MaintenanceBarrier(RegistryFile.path_in(self.root)).allow_input(),
            super().writer_connection(check_same_thread=check_same_thread) as connection,
        ):
            yield connection

    def allocate_problem(self) -> str:
        with self.writing() as db:
            return ProblemNumber.allocate(db)

    @staticmethod
    def require_unit_in(db: sqlite3.Connection, unit: ThreadIncarnation,
                        assigner: ThreadIncarnation) -> None:
        """The original open Todo owns who assigns its unit, under this transaction."""
        match = (f"({Todo.unit_index.term_sql(Todo)})="
                 f"({','.join('?' for _ in Todo.unit_index.terms)})")
        for state in TodoState.open_names():
            held = Todo.select(db,
                where=f"{match} AND NOT (creator=? AND creator_created=?)",
                parameters=(unit.name, unit.wire_birth, state, assigner.name, assigner.wire_birth),
                limit=1)
            for todo in held:
                raise TodoError(
                    f"{unit.name} is assigned by {todo.creator} on open order "
                    f"{todo.order.checked().id} ({todo.id}); "
                    "finish or release that order before assigning it again.")

    def record_hunt(self, commander: ThreadIncarnation, hunt: Hunt) -> HuntRecord:
        hunt.require_commander(self, commander)
        recorded = HuntRecord(uuid4().hex, commander, hunt)
        with self.writing() as db:
            recorded.upsert(db)
        return recorded

    def hunts_for_commander(self, commander: ThreadIncarnation) -> tuple[Hunt, ...]:
        with self.snapshot() as db:
            return tuple(record.hunt for record in HuntRecord.select(db, commander=commander))

    def resolve_order(self, commander: ThreadIncarnation, unit: str, problem: str) -> Todo:
        found = [t for t in self.held_by(commander, unit) if t.id == problem]
        if len(found) != 1:
            raise ValueError(f'{unit} holds no order on {problem}')
        return found[0]

    def drop(self, commander: ThreadIncarnation, unit: str, problem: str, by: str) -> Todo:
        old = self.resolve_order(commander, unit, problem)
        newer = self.resolve_order(commander, unit, by)
        if not old.pending or not newer.pending or newer.order.at.at <= old.order.at.at:
            raise ValueError('Only a live newer order to the same unit replaces an old one.')
        with self.writing() as db:
            current = self._current(db, old.id)
            current.require_result(old)
            self._current(db, newer.id).require_result(newer)
            result = replace(
                current,
                order=current.order.copy_as(
                    DroppedProblemOrder, replacing_message=newer.latest_message.message_id
                ),
                assignment=None,
                released=(
                    *current.released,
                    ReleaseTodo(
                        current.revision, commander, current.held(),
                        StrikeReason(f'Order {problem} replaced by newer {by} without a strike.'),
                    ),
                ),
                revision=current.revision + 1,
            )
            result.upsert(db)
            return result

    def fold(self, commander: ThreadIncarnation, problem: str, into: str, why: str) -> None:
        with self.writing() as db:
            source, target = self._current(db, problem), self._current(db, into)
            source.require_commander(commander)
            target.require_commander(commander)
            source.require_fold(target, why)
            replace(target, text=f'{target.text} [+{source.id}: {source.text}]',
                    revision=target.revision + 1).upsert(db)
            replace(source, state=DoneTodoState, order=source.order.folded(into, why),
                    assignment=None, revision=source.revision + 1).upsert(db)

    def link(
        self, commander: ThreadIncarnation, problem: str, unit: str, held_problem: str
    ) -> None:
        sent = self.resolve_order(commander, unit, held_problem)
        with self.writing() as db:
            source, target = self._current(db, sent.id), self._current(db, problem)
            source.require_result(sent)
            target.require_commander(commander)
            target.state.require_open(target)
            if target.holder not in (None, unit):
                raise ValueError(f'{problem} is already held by {target.holder}')
            linked = target.order.linked(source.order)
            replace(target, order=linked,
                    prior_orders=target.orders_before(linked, source.prior_orders),
                    assignment=source.held(), goal=source.goal, review_at=source.review_at,
                    messages=source.messages, revision=target.revision + 1).upsert(db)

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

    def for_order(self, order_id: str) -> tuple[Todo, ...]:
        """A sent order can cover several linked problems; its ID is not a Todo key."""
        rows = self._select("json_extract(\"order\",'$.id')=?", (order_id,))
        if not rows:
            raise TodoError(f"Unknown saved order: {order_id!r}")
        return rows

    def related_problems(self, problem: Todo) -> tuple[Todo, ...]:
        """Reissued orders retain the earlier declaration that linked their problem rows."""
        identities = problem.held_order_ids
        slots = ','.join('?' for _ in identities)
        return self._select(
            f'EXISTS (SELECT 1 FROM json_tree("order") AS declaration '
            f"WHERE declaration.key='id' AND declaration.value IN ({slots})) "
            f'OR EXISTS (SELECT 1 FROM json_each(prior_orders) AS declaration '
            f"WHERE json_extract(declaration.value,'$.id') IN ({slots}))",
            (*identities, *identities))

    def get(self, todo_id: str) -> Todo:
        _text(todo_id, "Todo ID", 128)
        return self._one(self._select("id=?", (todo_id,)), todo_id)

    def list(self, repo: RepositoryName) -> tuple[Todo, ...]:
        return self._select("repo=?", (repo.to_wire(),))

    def for_goal(self, owner: ThreadIncarnation, goal_id: str) -> tuple[Todo, ...]:
        return self._select(
            "json_extract(assignment,'$.owner')=? AND json_extract(assignment,'$.owner_created')=? "
            "AND json_extract(assignment,'$.generation')=?", (owner.name, owner.created_at, goal_id))

    def held_orders(self) -> tuple[Todo, ...]:
        return self._open("assignment IS NOT NULL", (), OpenTodoState)

    def reassignable(self) -> tuple[Todo, ...]:
        """Work Core released after its objective strikes; never a commander's released backlog.
        No clock releases work: a held order stays held until its commander moves it."""
        return tuple(todo for todo in self._open("assignment IS NULL", (), OpenTodoState)
                     if todo.pending and todo.handed_out_on_release)

    def merged_orders(self, commander: ThreadIncarnation,
                      registry: Registration) -> tuple[Todo, ...]:
        """Observe linked merges without holding a write lock."""
        held = self.held_for_commander(commander)
        pulls = PullRequests(held)
        return tuple(closed for todo in held
                     for closed in todo.order.closed_by_merge(todo, registry, pulls))

    def record_merges(self, completed: tuple[Todo, ...]) -> None:
        """Record only merge changes whose observed revision still holds."""
        if not completed:
            return
        with self.writing() as db:
            for result in completed:
                current = self._current(db, result.id)
                if current.revision + 1 == result.revision:
                    result.upsert(db)

    def board_disagreements(
        self, commander: ThreadIncarnation, registry: Registration,
    ) -> Iterator[tuple[Todo, MergedProgress]]:
        """Open orders whose progress misses a linked merge, sharing one PR read cache."""
        todos = self.held_for_commander(commander)
        pulls = PullRequests(todos)
        for todo in todos:
            progress = OrderProgress.read(todo, registry, pulls)
            source = ProgressSource.of(todo, registry, pulls)
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

    def timers_up_for_commander(self, commander: ThreadIncarnation, registry: Registration,
                                now: float) -> tuple[tuple[Todo, RecordedEventTime], ...]:
        """Held work whose review timer is up, with its round; merged work waits on install."""
        held = self.held_for_commander(commander)
        pulls = PullRequests(held)
        return tuple((todo, up) for todo in held for up in todo.timer_up(now)
                     if not todo.order.waiting_on_install(
                         todo, {todo.id: OrderProgress.read(todo, registry, pulls)}))

    def held_for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._open("creator=? AND creator_created=? AND assignment IS NOT NULL",
            (commander.name, commander.wire_birth))

    def held_by(self, commander: ThreadIncarnation, unit: str) -> tuple[Todo, ...]:
        return self._open(
            "creator=? AND creator_created=? AND json_extract(assignment,'$.owner')=?",
            (commander.name, commander.wire_birth, unit))

    def require_recurrence_declaration(
        self, order: CheckedProblemOrder, where: FileLine, root: Path,
        slop: tuple[Slop, ...], now: float,
    ) -> None:
        previous = tuple(line for todo in self.list(Repo.of(root).github)
                         for line in todo.recurrence((order.owner, where), root, now))
        if previous and not any(
            item.derive.declaration_elsewhere(item.by, where, root) for item in slop
        ):
            raise TodoError('\n'.join((*previous,
                'the same file broke again, name the fact written in more than one place '
                'and the declaration that replaces the copies.')))

    def history_for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._select("creator=? AND creator_created=?", (commander.name, commander.wire_birth))

    def units(self, commander: ThreadIncarnation) -> frozenset[str]:
        """The threads this commander gives orders to: everyone who holds or held one of its
        problems."""
        return frozenset(
            assignment.owner for todo in self.history_for_commander(commander)
            for assignment in (*todo.holder_state.current_assignments(),
                               *(release.previous for release in todo.released)))

    def _open(self, where: str, parameters: tuple,
              state: type[TodoState] = TodoState) -> tuple[Todo, ...]:
        states = state.open_names()
        selected = ','.join('?' for state in states)
        return self._select(f"state IN ({selected}) AND ({where})", (*states, *parameters))

    def create(
        self,
        todo_id: str,
        repo: RepositoryName,
        text: str,
        *,
        creator: Thread,
        owner: Thread,
        generation: str,
        review_at: EventTime,
        order: PendingProblemOrder,
        goal: GoalRef,
    ) -> Todo:
        identity, assignee = _participant(creator), _thread(owner)
        DirectiveText.require(text)
        order.require_wording()
        order.require_writable()
        proposed = Todo.ordered(
            todo_id, repo, text, identity, Assignment.of(assignee, identity, generation),
            review_at, order, goal)
        with self.writing() as db:
            self.require_unit_in(db, assignee, identity)
            ProblemNumber.reserve(db, todo_id)
            proposed.insert_if_absent(db)
            current = self._current(db, todo_id)
            current.require_same_creation(proposed)
            current.require_assignment(proposed.assignment)
            return current

    def apply(self, todo_id: str, command: TodoCommand) -> Todo:
        with self.writing() as db:
            current = self._current(db, todo_id)
            command.require_unit(db, current)
            result = command.apply(current)
            if result == current:
                return current
            result.upsert(db)
            return self._current(db, todo_id)

    def correct(self, unit: str, message_id: str, text: str) -> tuple[Todo, ...]:
        """Correct every open order the unit holds when its published text uses jargon."""
        if not self.path.exists() or not (words := tuple(PlainText.found(text))):
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
            held = tuple((todo, todo.held()) for todo in self._rows(
                db, "json_extract(assignment,'$.owner')=? AND creator=? AND creator_created=?",
                (unit, actor.name, actor.wire_birth))
                if todo.state.open)
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
        assigner: ThreadIncarnation,
        generation: str,
        goal: GoalRef,
        review_at: EventTime,
        order: PendingProblemOrder,
        traced: FileLine | NoSourceReference = NoSourceReference(),
    ) -> Todo:
        proposed = Assignment.of(_thread(owner), assigner, generation)
        return self.apply(
            todo_id, AssignTodo(expected_revision, proposed, review_at, order, goal, traced))
