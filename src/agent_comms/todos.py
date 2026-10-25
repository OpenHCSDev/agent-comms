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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from functools import cached_property, partial
from math import ceil
from pathlib import Path
from statistics import median
from types import get_original_bases
from typing import TYPE_CHECKING, Annotated, ClassVar, Self, get_args, get_origin
from uuid import uuid4

from agent_comms.build_identity import (
    BuildIdentity,
    CommitInstallation,
    RecordedBuildIdentity,
    UnrecordedBuildIdentity,
)
from agent_comms.bus_publication import AgentOrigin
from agent_comms.change_report import (
    CitedRevisions,
    CommitRevision,
    FileLine,
    MainRevision,
    NoSourceReference,
    PullRequestHeadRevision,
    SourceReference,
    SourceRevision,
    SourceText,
    WorkingTree,
)
from agent_comms.channel_targets import CommandTarget, CommsTag, IncarnationThreadTarget, NamedTarget
from agent_comms.child_process import (
    CheckPrinted,
    ChildResult,
    NoIntegerPrinted,
    OrderCheckFailedError,
)
from agent_comms.command import Command
from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import RuleAudience, RuleFile
from agent_comms.coordination_session import NativeBoundary
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import MissingSourceLine, TodoConflict, TodoError, ToolRefusedError
from agent_comms.field_codec import (
    DefaultNonNull,
    Description,
    GoalId,
    FieldCodec,
    GitCommitText,
    MalformedRecord,
    MessageId,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    WireOmitDefault,
    WireTransient,
    WireValue,
    boundary_decoder,
)
from agent_comms.goals import CheckedCommit, OrderCheck, ProblemGoalOrder
from agent_comms.repo import GitCommandError
from agent_comms.head_review import PullRequestHead
from agent_comms.lifecycle_capability import LifecycleCapability, Pending
from agent_comms.message_reference import MessageReference
from agent_comms.messages import InfoKind, Message
from agent_comms.tool_denial import SelectedToolDenied
from agent_comms.order_runs import OrderRun
from agent_comms.order_story import ProblemStory, StoryEvent, StoryLine, StorySource
from agent_comms.owner_requests import (
    OpenRequestState,
    OwnerRequestId,
    RequestAtRevision,
    RequestDirective,
    RequestLink,
    RequestSelection,
    RequestState,
)
from agent_comms.plan_phases import OrderPhase, UnplannedPhase
from agent_comms.head_review import HeadReview
from agent_comms.problem_number import ProblemNumber
from agent_comms.progress_claims import MergedClaim, ProgressClaim, WorkingClaim
from agent_comms.recorded_time import (
    EventTime,
    RecordedEventTime,
    UnrecordedEventTime,
)
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.slop import Slop
from agent_comms.storage_rules import StorageRule, UnitStorage
from agent_comms.store_files import SharedLock
from agent_comms.thread_identity import (
    CoreWatcherIncarnation,
    RecordedThreadIncarnation,
    ThreadIncarnation,
)
from agent_comms.threads import Thread
from agent_comms.typed_table import (
    StoreRows,
    TableReference,
    Column,
    DurableStore,
    DurableTables,
    ExpressionIndex,
    Generated,
    Index,
    SharedFileTables,
    TextStorage,
    TypedTable,
)
from .goal_scope import OneGoal
from agent_comms.work_branches import (
    BranchComparison, BranchDifference, SourceGrowth, WorkBranchError, WorkBranches,
)

if TYPE_CHECKING:
    from agent_comms.commander.order import (
        Commander, CommanderContext, OrderedProblems, OrderFile, Reassignment,
    )
    from agent_comms.bus import BusReader
    from agent_comms.comms import Comms
    from agent_comms.goal_management import Goals
    from agent_comms.registration import Registration
    from agent_comms.registry_document import RegistrySnapshot


@dataclass(frozen=True)
class DeliveryEvidence(DeclaredFamily, affix="DeliveryEvidence"):
    def artifact(self) -> str:
        raise TodoError("No application evidence was supplied at delivery; pass --artifact FILE.")

    def require_run(self, run: OrderRun) -> None:
        run.require_artifact("")


@dataclass(frozen=True)
class NoDeliveryEvidence(DeliveryEvidence):
    pass


@dataclass(frozen=True)
class FileDeliveryEvidence(DeliveryEvidence):
    path: str

    def artifact(self) -> str:
        if not Path(self.path).is_file():
            raise TodoError("Delivery evidence must name an existing file.")
        return self.path

    def require_run(self, run: OrderRun) -> None:
        run.require_artifact(self.path)


class FreshMainCheck:
    """Undelivered work is checked against fetched main, independent of the local branch."""

    def check_base(self, repo: Path) -> str:
        return f"origin/{WorkBranches.main}"


class BranchPointCheck:
    """Delivered work is checked against the main commit it was built on, never main since:
    once the fix merges, current main passes, which proves nothing about the check."""

    @property
    @abstractmethod
    def work_commit(self) -> str: ...

    def check_base(self, repo: Path) -> str:
        main = f"origin/{WorkBranches.main}"
        line = set(Repo.git(repo, "rev-list", "--first-parent", main).split())
        for commit in Repo.git(repo, "rev-list", "--first-parent", f"{self.work_commit}^1").split():
            if commit in line:
                return commit
        raise TodoError(f"{self.work_commit} was not built on {WorkBranches.main}.")


@dataclass(frozen=True, kw_only=True)
class OrderDelivery(DeclaredFamily, StoryEvent, affix="Delivery"):
    at: EventTime = field(default_factory=UnrecordedEventTime)

    def unobserved(self) -> OrderDelivery:
        """The delivered work without the observations of the proof that recorded it."""
        return replace(self, at=UnrecordedEventTime())

    def reproved(self, delivery: OrderDelivery) -> OrderDelivery:
        if delivery.unobserved() == self.unobserved():
            return delivery
        raise TodoError("The order already records a different delivery.")

    def check_base(self, repo: Path) -> str:
        raise TodoError("This delivery has no source commit to establish a check base.")

    def done_claims(self, order: CheckedProblemOrder) -> tuple[ProgressClaim, ...]:
        return ()

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
        raise TodoError("A triage report is not installed application evidence.")

    @property
    def installation_build(self) -> BuildIdentity:
        raise TodoError("A triage report cannot be installed.")

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

    def unobserved(self) -> OrderDelivery:
        return replace(self, at=UnrecordedEventTime(), evidence=NoDeliveryEvidence(),
                       proof_build=UnrecordedBuildIdentity())

    def closing_artifact(self) -> str:
        return self.evidence.artifact()

    def done_claims(self, order: CheckedProblemOrder) -> tuple[ProgressClaim, ...]:
        self.evidence.require_run(order.run)
        return super().done_claims(order)

    @property
    def installation_build(self) -> BuildIdentity:
        return self.proof_build

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
        revision = self.source_revision()
        for line in order.created:
            try:
                await asyncio.to_thread(line.require_exists, check.directory, revision)
            except MissingSourceLine as error:
                raise TodoError(f"The order creates {line.to_wire()}; {error}") from error
        return replace(self, at=await self.delivery_time(check),
                       proof_build=await asyncio.to_thread(self.proof_build.recorded))

    def source_revision(self) -> SourceRevision:
        """The tree the delivery's checks ran on."""
        return MainRevision()

    @abstractmethod
    async def require_source(self, check): ...

    @abstractmethod
    async def checked(self, check: OrderCheck, checkout_base: Path) -> ChildResult: ...

    @abstractmethod
    async def delivery_time(self, check): ...


@dataclass(frozen=True, kw_only=True)
class RecordedCommitDelivery(BranchPointCheck, CheckedDelivery, declared_name="commit"):
    """A delivery recorded before checked and delivered commits were told apart.

    Its one commit is both facts; Core refuses new deliveries of this kind."""
    commit: str

    def __post_init__(self) -> None:
        GitCommitText.decode(self.commit)
        GitCommitText.decode(self.delivered)

    @property
    def work_commit(self) -> str:
        return self.delivered

    def done_claims(self, order: CheckedProblemOrder) -> tuple[ProgressClaim, ...]:
        super().done_claims(order)
        return (MergedClaim(self.delivered),)

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

    def source_revision(self) -> SourceRevision:
        return CommitRevision(self.commit)

    async def require_source(self, check: OrderCheck) -> None:
        raise TodoError("A commit delivery names the unit's delivered commit and the checked one.")

    async def checked(self, check: OrderCheck, checkout_base: Path) -> ChildResult:
        return await asyncio.to_thread(check.at, self.commit, check.directory, checkout_base)

    async def delivery_time(self, check: OrderCheck) -> EventTime:
        return EventTime.capture(float(
            await asyncio.to_thread(Repo.git, check.directory, "show", "-s", "--format=%ct",
                                    self.delivered)))


@dataclass(frozen=True, kw_only=True)
class CommitDelivery(RecordedCommitDelivery, declared_name="delivered_commit"):
    """The check runs on ``commit``; ``delivered_commit`` is the unit's pushed ancestor of it."""
    delivered_commit: str

    @property
    def delivered(self) -> str:
        return self.delivered_commit

    async def require_source(self, check: OrderCheck) -> None:
        delivered = (await asyncio.to_thread(
            Repo.git, check.directory, "rev-parse", f"{self.delivered}^{{commit}}")).strip()
        base = await asyncio.to_thread(Repo.git, check.directory, "merge-base", self.delivered,
                                       self.commit)
        if base.strip() != delivered:
            raise TodoError(
                f"Delivered commit {self.delivered} is not in the checked commit {self.commit}.")

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
            WorkBranches(check.directory).read, registry.snapshot())
        sources = tuple(sorted(branch.name for branch in linked
                               if branch.name in branches and branch.linked))
        if not sources:
            raise TodoError(
                f"Delivered commit {self.delivered} is not merged and has no branch linked "
                "to an open PR or issue.")
        return UnmergedDeliveryMerge(sources)

    async def contains(self, check: OrderCheck, head: str) -> bool:
        if head == self.delivered:
            return True
        return await asyncio.to_thread(Repo.descends, check.directory, self.delivered, head)


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
class OperationDelivery(FreshMainCheck, CheckedDelivery):
    def done_claims(self, order: CheckedProblemOrder) -> tuple[ProgressClaim, ...]:
        super().done_claims(order)
        return (WorkingClaim(self.evidence.artifact()),)

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
            raise TodoError("report must contain the triage findings")

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
            raise TodoError("A pull request number must be positive.")

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
            raise TodoError(f"GitHub did not report pull request {number} in {repo.to_wire()}.")
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
    status_contexts: tuple[str, ...] = field(default=(), kw_only=True)

    def as_ready(self) -> tuple[PullRequest, ...]:
        return (self,)

    def as_draft(self) -> tuple[PullRequest, ...]:
        return ()

    def head_review_due(self, now: float, due_at: float) -> tuple[float, ...]:
        return () if self.has_head_review or now < due_at else (due_at,)

    def empty_changes(self, reader: WorkBranches) -> tuple[BranchComparison, ...]:
        compared = reader.pull_request_diff(self.base, self.head)
        return () if compared.paths else (compared,)

    @property
    def has_head_review(self) -> bool:
        return HeadReview.context in self.status_contexts

    def belongs_to(self, todo: Todo) -> bool:
        return (todo.id in self.problems
                or any(identity in self.orders for identity in todo.held_order_ids))

    def merged_commits(self, since: float) -> Iterator[str]:
        return iter(())

    def delivery(self, repo: Path, evidence: DeliveryEvidence) -> CommitDelivery:
        raise TodoError(f"Pull request {self.number} is not merged; done checks the merged tree.")

    async def delivery_merges(self, delivery: CommitDelivery,
                              check: OrderCheck) -> tuple[DeliveryMerge, ...]:
        return ()


class UnmergedPullRequest(PullRequest):
    """An open or closed request has no merge commit or integration time."""

    @classmethod
    def from_listed(cls, row: ListedPullRequest, problems: tuple[str, ...],
                    orders: tuple[str, ...]) -> Self:
        return cls(row.number, row.branch, row.head, row.updated_at, orders,
                   base=row.base, problems=problems, status_contexts=row.status_contexts)


class DraftPullRequest(UnmergedPullRequest):
    """A draft promises neither a change nor a reviewable head."""

    def as_ready(self) -> tuple[PullRequest, ...]:
        return ()

    def as_draft(self) -> tuple[PullRequest, ...]:
        return (self,)

    def head_review_due(self, now: float, due_at: float) -> tuple[float, ...]:
        return ()

    def empty_changes(self, reader: WorkBranches) -> tuple[BranchComparison, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class MergedPullRequest(PullRequest):
    merge: str
    merged_at: float

    def merged_commits(self, since: float) -> Iterator[str]:
        if self.merged_at >= since:
            yield self.merge

    def delivery(self, repo: Path, evidence: DeliveryEvidence) -> CommitDelivery:
        """The merged tree is the delivered tree; the request's first commit is the unit's work."""
        Repo.git(repo, "fetch", "-q", "origin", self.base, f"refs/pull/{self.number}/head")
        for first in Repo.git(repo, "rev-list", "--first-parent", "--reverse",
                              f"{self.merge}^1..{self.head}").split():
            return CommitDelivery(commit=self.merge, delivered_commit=first, evidence=evidence)
        raise TodoError(f"Pull request {self.number} adds no commit to {self.base}.")

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
class MergeBlock(DeclaredFamily, affix="MergeBlock"):
    """Why GitHub will not merge a head, decoded once from the forge row."""
    at: str

    @cached_property
    def since(self) -> float:
        return _github_time(self.at)

    @abstractmethod
    def what(self) -> str: ...


@dataclass(frozen=True)
class FailedCheckMergeBlock(MergeBlock):
    """A required check concluded red on the head."""
    name: str

    def what(self) -> str:
        return f"required check {self.name} failed"


@dataclass(frozen=True)
class ConflictMergeBlock(MergeBlock):
    """The head conflicts with its base; no check can turn it green."""

    def what(self) -> str:
        return "conflicts with its base"


@dataclass(frozen=True)
class ListedPullRequest(DeclaredFamily, affix="ListedPullRequest"):
    """The external query row; conversion lives here, never in its consumers."""
    number: int
    branch: str
    head: str
    updated: str
    base: str
    reviewable_times: tuple[str, ...] = field(kw_only=True)
    status_contexts: tuple[str, ...] = field(default=(), kw_only=True)
    armed_times: tuple[str, ...] = field(default=(), kw_only=True)
    merge_blocks: tuple[MergeBlock, ...] = field(default=(), kw_only=True)

    def retained_assignments(self, assignment: Assignment) -> tuple[Assignment, ...]:
        """Open work and merged work awaiting acceptance stay with their recorded holder."""
        return (assignment,)

    def as_unmerged(self) -> tuple[UnmergedListedPullRequest, ...]:
        return ()

    def stuck(self) -> tuple[MergeBlock, ...]:
        """Blocks on a head whose auto-merge is armed: queued, and going nowhere."""
        return tuple(block for _ in self.armed_times for block in self.merge_blocks)

    def as_merged(self) -> tuple[MergedListedPullRequest, ...]:
        return ()

    def as_closed(self) -> tuple[ClosedListedPullRequest, ...]:
        return ()

    @classmethod
    @abstractmethod
    def picked(cls, row: ListedPullRequest) -> tuple[Self, ...]: ...

    @cached_property
    def updated_at(self) -> float:
        return _github_time(self.updated)

    @cached_property
    def reviewable_at(self) -> float:
        return max(map(_github_time, self.reviewable_times))

    def review_due(self, now: float, seconds: float) -> tuple[float, ...]:
        due = self.reviewable_at + seconds
        return () if now < due else (due,)

    def parsed(self, problems: tuple[str, ...], orders: tuple[str, ...]) -> PullRequest:
        return UnmergedPullRequest.from_listed(self, problems, orders)


class UnmergedListedPullRequest(ListedPullRequest):
    """An open request, selected by its declared member rather than a state string."""

    def as_unmerged(self) -> tuple[UnmergedListedPullRequest, ...]:
        return (self,)

    @classmethod
    def picked(cls, row: ListedPullRequest) -> tuple[UnmergedListedPullRequest, ...]:
        return row.as_unmerged()


class DraftListedPullRequest(UnmergedListedPullRequest):
    """An open draft is selectable but never due for review."""

    def review_due(self, now: float, seconds: float) -> tuple[float, ...]:
        return ()

    def parsed(self, problems: tuple[str, ...], orders: tuple[str, ...]) -> DraftPullRequest:
        return DraftPullRequest.from_listed(self, problems, orders)


class ClosedListedPullRequest(ListedPullRequest):
    """Closed without merging; never selected as an open request."""

    def retained_assignments(self, assignment: Assignment) -> tuple[Assignment, ...]:
        return ()

    def as_closed(self) -> tuple[ClosedListedPullRequest, ...]:
        return (self,)

    @classmethod
    def picked(cls, row: ListedPullRequest) -> tuple[ClosedListedPullRequest, ...]:
        return row.as_closed()


@dataclass(frozen=True, kw_only=True)
class MergedListedPullRequest(ListedPullRequest):
    merge: PullRequestCommit
    merged: str

    def as_merged(self) -> tuple[MergedListedPullRequest, ...]:
        return (self,)

    @classmethod
    def picked(cls, row: ListedPullRequest) -> tuple[MergedListedPullRequest, ...]:
        return row.as_merged()

    def parsed(self, problems: tuple[str, ...], orders: tuple[str, ...]) -> MergedPullRequest:
        return MergedPullRequest(self.number, self.branch, self.head, self.updated_at,
                                 orders, base=self.base,
                                 merge=self.merge.oid, merged_at=_github_time(self.merged),
                                 problems=problems, status_contexts=self.status_contexts)


class PullRequests:
    """GitHub facts for recorded links, cached within one board read."""

    def __init__(self, todos: Sequence[Todo] = ()) -> None:
        self._read: dict[tuple[RepositoryName, int], ListedPullRequest] = {}
        self._open: dict[RepositoryName, tuple[int, ...]] = {}
        self._todos: dict[RepositoryName, dict[str, Todo]] = {}
        self.register(todos)

    def register(self, todos: Sequence[Todo]) -> Self:
        """Register a frame's orders while preserving this read's existing GitHub facts."""
        for todo in todos:
            self._todos.setdefault(todo.repo, {})[todo.id] = todo
        return self

    def retained_assignments(self, todo: Todo, assignment: Assignment) -> tuple[Assignment, ...]:
        """An explicit closure releases work; missing forge links do not retire its author."""
        self.register((todo,))
        linked = self.linked(todo.repo)
        numbers = tuple(number for number, candidates in linked.items() if todo in candidates)
        if not numbers:
            return (assignment,)
        return tuple(dict.fromkeys(
            retained for row in self.rows(todo.repo, numbers)
            for retained in row.retained_assignments(assignment)))

    def merged_delivery(self, todo: Todo, repo: Path, evidence: DeliveryEvidence) -> CommitDelivery:
        """The newest merged request linked to the order is what `order done` checks."""
        for request in self.listed(todo.repo, MergedListedPullRequest, todo):
            return request.delivery(repo, evidence)
        raise TodoError(
            f"{todo.id} has no merged pull request; link it with `order pr` and merge it.")

    def claimed_heads(self, todo: Todo) -> Iterator[tuple[OrderClaim, PullRequest]]:
        """Join the current claim to its open PR in this same GitHub read."""
        for claim in todo.order.owed_claims():
            for request in self.listed(claim.pull_request.repo, UnmergedListedPullRequest, todo):
                if request.number == claim.pull_request.number:
                    yield claim, request

    def linked(self, repo: RepositoryName) -> dict[int, list[Todo]]:
        linked: dict[int, list[Todo]] = {}
        for candidate in self._todos.get(repo, {}).values():
            for number in {link.number for order in candidate.order.declarations
                           for link in order.linked_requests
                           if link.repo == repo}:
                linked.setdefault(number, []).append(candidate)
        return linked

    def rows(self, repo: RepositoryName, numbers: tuple[int, ...]) -> tuple[ListedPullRequest, ...]:
        missing = tuple(number for number in numbers if (repo, number) not in self._read)
        if missing:
            self._read.update(((repo, row.number), row) for row in self.query(repo, missing))
        return tuple(self._read[repo, number] for number in numbers)

    def open(self, repo: RepositoryName) -> tuple[UnmergedListedPullRequest, ...]:
        """Discover orderless open identities, then use the same metadata owner as linked PRs."""
        if repo not in self._open:
            result = repo.rest("pulls", "--paginate", "--jq", ".[].number")
            if result.returncode:
                raise TodoError(result.stderr.strip() or "Open pull requests could not be read.")
            self._open[repo] = tuple(FieldCodec.decode(int, FieldCodec.parse(line))
                                     for line in result.stdout.splitlines())
        numbers = tuple(dict.fromkeys((*self._open[repo], *self.linked(repo))))
        self.rows(repo, numbers)
        return tuple(picked for number in self._open[repo]
                     for picked in self._read[repo, number].as_unmerged())

    def listed(self, repo: RepositoryName, kind: type[ListedPullRequest],
               todo: Todo) -> tuple[PullRequest, ...]:
        self.register((todo,))
        linked = self.linked(repo)
        requests = tuple(picked.parsed(
            tuple(candidate.id for candidate in linked[picked.number]),
            tuple(dict.fromkeys(order.id for candidate in linked[picked.number]
                                for order in candidate.order.declarations
                                if PullRequestLink(repo, picked.number) in order.linked_requests)))
            for row in self.rows(repo, tuple(linked)) for picked in kind.picked(row))
        return tuple(sorted((request for request in requests if request.belongs_to(todo)),
                            key=lambda request: request.updated_at, reverse=True))

    def query(self, repo: RepositoryName,
              numbers: tuple[int, ...]) -> tuple[ListedPullRequest, ...]:
        """GitHub's rows for these numbers: the one forge read in this frame."""
        selections = [
            f'p{number}: pullRequest(number: {number}) {{ number branch: headRefName '
            'head: headRefOid base: baseRefName draft: isDraft closed merge: mergeCommit { oid } '
            'merged: mergedAt updated: updatedAt created: createdAt '
            'ready_events: timelineItems(itemTypes: [READY_FOR_REVIEW_EVENT], last: 1) '
            '{ nodes { ... on ReadyForReviewEvent { createdAt } } } '
            'auto: autoMergeRequest { enabledAt } merge_state: mergeStateStatus '
            'head_statuses: commits(last: 1) { nodes { commit { committedDate status { contexts { context } } '
            'statusCheckRollup { contexts(first: 100) { nodes { '
            f'... on CheckRun {{ name conclusion completedAt isRequired(pullRequestNumber: {number}) }} '
            f'... on StatusContext {{ context state createdAt isRequired(pullRequestNumber: {number}) }} '
            '} } } } } } }'
            for number in numbers]
        query = (f'query {{ repository(owner: {json.dumps(repo.owner)}, '
                 f'name: {json.dumps(repo.name)}) {{ {" ".join(selections)} }} }}')
        listed = repo.api(
            "graphql", "-f", "query=" + query,
            "--jq", '.data.repository | map_values((if .merged == null then '
            '(if .closed then '
            f'del(.merge,.merged) + {{kind: "{ClosedListedPullRequest.declared_name}"}} else '
            '(if .draft then '
            f'del(.merge,.merged) + {{kind: "{DraftListedPullRequest.declared_name}"}} else '
            f'del(.merge,.merged) + {{kind: "{UnmergedListedPullRequest.declared_name}"}} end) end) else '
            f'. + {{kind: "{MergedListedPullRequest.declared_name}"}} end) '
            '| . + {status_contexts: [.head_statuses.nodes[].commit.status.contexts[]?.context], '
            'reviewable_times: [.created, .head_statuses.nodes[].commit.committedDate, '
            '.ready_events.nodes[].createdAt], '
            'armed_times: [.auto.enabledAt? // empty], '
            'merge_blocks: ([.head_statuses.nodes[].commit.statusCheckRollup.contexts.nodes[]? '
            '| select(.isRequired) | select((.conclusion // .state) as $c '
            '| ["FAILURE","TIMED_OUT","CANCELLED","ACTION_REQUIRED","STARTUP_FAILURE","ERROR"] '
            '| index($c)) '
            f'| {{kind: "{FailedCheckMergeBlock.declared_name}", name: (.name // .context), '
            'at: (.completedAt // .createdAt)}] '
            f'+ [select(.merge_state == "DIRTY") | {{kind: "{ConflictMergeBlock.declared_name}", '
            'at: .updated}])} '
            '| del(.draft,.closed,.created,.head_statuses,.ready_events,.auto,.merge_state))')
        if listed.returncode:
            raise WorkBranchError(
                listed.stderr.strip() or f"GitHub order lookup in {repo.to_wire()} failed.")
        return tuple(FieldCodec.decode(
            dict[str, ListedPullRequest], FieldCodec.parse(listed.stdout)).values())


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

    def require_read(self) -> None:
        """A retired checkout needs no local read; present and unread ones decide."""

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


@dataclass(frozen=True)
class UnreadCheckout(ProgressCheckout):
    """A failed source read is data, never a missing checkout or no progress."""

    reason: str

    def require_read(self) -> None:
        raise WorkBranchError(self.reason)

    def commits(self, pattern: str, refs: tuple[str, ...],
                *excluded: str) -> Iterator[ProgressPoint]:
        raise WorkBranchError(self.reason)

    def references(self, storage: StorageRule, owner: Thread) -> tuple[str, ...]:
        return ()

    def readings(self, source: ProgressSource) -> DistanceReadings:
        raise WorkBranchError(self.reason)

    def pushes(self, pattern: str) -> Iterator[tuple[str, float]]:
        raise WorkBranchError(self.reason)


class PresentCheckout(ProgressCheckout):
    default_reference: ClassVar[str] = "refs/remotes/origin/HEAD"

    @property
    def default_refs(self) -> tuple[str, ...]:
        return (self.default_reference,) if self.default_reference in self.git_refs else ()

    def git(self, *args: str) -> str:
        return Repo.git(self.path, *args)

    def require_read(self) -> None:
        self.git("rev-parse", "--git-dir")

    @cached_property
    def git_refs(self) -> tuple[str, ...]:
        return tuple(self.git("for-each-ref", "--format=%(refname)",
                              "refs/heads/", "refs/remotes/origin/").splitlines())

    @cached_property
    def origin_refs(self) -> tuple[str, ...]:
        return tuple(ref for ref in self.git_refs if ref.startswith("refs/remotes/origin/"))

    @cached_property
    def origin_pushes(self) -> tuple[tuple[str, float], ...]:
        """Each push to a retained origin reference, as its commit and push time."""
        origin = frozenset(self.origin_refs)
        pushes = []
        for row in self.git("reflog", "show", "--all", "--date=unix",
                            "--grep-reflog=update by push", "--format=%gD%x09%H").splitlines():
            selector, sha = row.split("\t")
            reference, stamp = selector.rsplit("@{", 1)
            if reference in origin:
                pushes.append((sha, float(stamp.removesuffix("}"))))
        return tuple(pushes)

    @cached_property
    def branches(self) -> WorkBranches:
        """One Git branch reader for this checkout during the progress frame."""
        return WorkBranches(self.path)

    @cached_property
    def checked_out(self) -> dict[str, tuple[str, ...]]:
        """Branch name -> the worktree paths that have it checked out."""
        return self.branches.worktrees()

    @cached_property
    def check_notes(self) -> dict[str, tuple[CheckedCommit, ...]]:
        """The stored check results read so far, by commit."""
        return {}

    def stored(self, sha: str) -> tuple[CheckedCommit, ...]:
        if sha not in self.check_notes:
            self.check_notes[sha] = CheckedCommit.stored(self.path, sha)
        return self.check_notes[sha]

    def references(self, storage: StorageRule, owner: Thread) -> tuple[str, ...]:
        owned = {f"refs/heads/{branch}"
                 for branch, paths in self.checked_out.items()
                 if any(storage.owns_present_checkout(owner, checkout)
                        for checkout in (Path(path).resolve() for path in paths))}
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
                if order.check.recorded_among(self.stored(point.sha)))

    def published(self, pattern: str) -> Iterator[ProgressPoint]:
        return self.commits(pattern, self.origin_refs, *self.default_refs)

    @cached_property
    def edits(self) -> tuple[float, ...]:
        """When each uncommitted change in this checkout was last written; a deletion has none."""
        return tuple(path.stat().st_mtime
                     for path in self.branches.read_checkout().uncommitted()
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
        for run in order.check.recorded_among(self.stored(point.sha)):
            touched = self.git("diff", "--name-only", base, point.sha).split()
            yield DistanceReading(point.at, order.check.result.distance(run).plus(
                order.untouched(touched)), order.check.command)

    def pushes(self, pattern: str) -> Iterator[tuple[str, float]]:
        """Retained origin commits prove publication; their push reflog supplies its clock."""
        published = {point.sha for point in self.published(pattern)}
        return ((sha, at) for sha, at in self.origin_pushes if sha in published)


class CheckoutReads:
    """Each checkout's Git facts within one read: orders sharing a checkout ask Git its
    refs and push reflog once, and nothing outlives the read to go stale."""

    def __init__(self) -> None:
        self._checkouts: dict[Path, PresentCheckout] = {}
        self._branch_mains: dict[RepositoryName, str] = {}
        self._branch_facts: dict[Path, BranchDifference] = {}

    def checkout(self, path: Path) -> PresentCheckout:
        """This read's one reader of the checkout at path."""
        return self._checkouts.setdefault(path, PresentCheckout(path))


    def branch_difference(self, path: Path) -> BranchDifference:
        """All consumers share each branch read and one main commit per repository."""
        path = path.resolve()
        if path not in self._branch_facts:
            reader = self.checkout(path).branches
            repository = RepositoryName(*reader.repository)
            if repository not in self._branch_mains:
                self._branch_mains[repository] = reader.origin_main
            self._branch_facts[path] = reader.difference(self._branch_mains[repository])
        return self._branch_facts[path]


class ProgressRead(PullRequests, CheckoutReads):
    """One read of order progress: its GitHub facts and its checkouts' Git facts."""

    def __init__(self, todos: Sequence[Todo] = ()) -> None:
        CheckoutReads.__init__(self)
        PullRequests.__init__(self, todos)
        self._progress: dict[str, OrderProgress] = {}

    def progress(self, held: Sequence[Todo], registry: Registration) -> dict[str, OrderProgress]:
        """Each held order's progress, read once per frame however many readers ask."""
        self.register(held)
        for todo in held:
            if todo.id not in self._progress:
                self._progress[todo.id] = OrderProgress.read(todo, registry, self)
        return {todo.id: self._progress[todo.id] for todo in held}


@dataclass(frozen=True)
class PullRequestSource:
    """The forge facts of one order: its linked pull requests, read without any holder."""

    todo: Todo
    pulls: ProgressRead

    @property
    def since(self) -> float:
        return self.todo.order.checked().at.recorded_at("An order")

    def requests(self, kind: type[ListedPullRequest]) -> list[PullRequest]:
        """Explicit PR references survive checkout removal or branch reuse."""
        return list(self.pulls.listed(self.todo.repo, kind, self.todo))


@dataclass(frozen=True)
class ProgressSource(PullRequestSource):
    """Problem progress across owned worktrees, shared Git history and retained PR identities."""

    checkout: ProgressCheckout
    references: tuple[str, ...] = ()
    worktrees: tuple[PresentCheckout, ...] = ()

    @classmethod
    def of(cls, todo: Todo, registry: Registration, pulls: ProgressRead) -> Self:
        owner = registry.require(todo.held().owner)
        storage = registry.storage_rule
        owned = storage.checkout_paths(owner)
        candidates = (todo.order.checked().check.directory, *owned)
        path = next((path for path in candidates if (path / ".git").exists()),
                    next(iter(owned), candidates[0]))
        checkout: ProgressCheckout = (
            pulls.checkout(path) if (path / ".git").exists() else MissingCheckout(path))
        try:
            references = checkout.references(storage, owner)
        except (OSError, GitCommandError, WorkBranchError, subprocess.TimeoutExpired) as failure:
            checkout, references = UnreadCheckout(path, str(failure)), ()
        return cls(
            todo=todo, pulls=pulls, checkout=checkout, references=references,
            worktrees=tuple(pulls.checkout(path) for path in owned if (path / ".git").exists()),
        )

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
        return tuple(sorted((edited for worktree in self.worktrees for edited in worktree.edits),
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
    def read(cls, todo: Todo, registry: Registration, pulls: ProgressRead) -> OrderProgress:
        """The first found member by its declared precedence; with none found, no commit yet."""
        return cls.from_source(MeasuredProgressSource.of(todo, registry, pulls))

    @classmethod
    def from_source(cls, source: ProgressSource) -> OrderProgress:
        members = cls.members_with(FoundProgress)
        try:
            return next((progress for member in members
                         for progress in member.observations(source)), NoCommitProgress())
        except (OSError, GitCommandError, WorkBranchError, subprocess.TimeoutExpired) as unread:
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

    @property
    def judgeable(self) -> bool:
        """Whether a head verdict can still gate this order's work: until its PR merges."""
        return True

    def missing_merges(self, source: ProgressSource) -> tuple[MergedProgress, ...]:
        """Linked merges this progress incorrectly reports as no commit."""
        return ()


@dataclass(frozen=True)
class FoundProgress(OrderProgress):
    """A state read finds in the checkout or on GitHub; the first declaration found wins."""

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
class MergedProgress(FoundProgress):
    """The forge proves a linked merge; the order's run owns what still needs doing."""
    sha: str

    @classmethod
    def found(cls, source: PullRequestSource) -> Iterator[Self]:
        """Merged only once every linked request has merged; one still open holds the order."""
        merged = (() if source.requests(UnmergedListedPullRequest)
                  else source.requests(MergedListedPullRequest))
        return (cls(sha) for pr in merged for sha in pr.merged_commits(source.since))

    @property
    def state(self) -> str:
        return f"merged as {self.sha[:9]}"

    def waiting_on_install(self, todo: Todo) -> tuple[Todo, ...]:
        return (todo,)

    @property
    def judgeable(self) -> bool:
        """A merged head has nothing left for a verdict to gate."""
        return False


@dataclass(frozen=True)
class OpenPrProgress(GitProgress):
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
class CommittedProgress(GitProgress):
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
class EditingProgress(Moving, NoCommitProgress, FoundProgress):
    """Uncommitted changes in the holder's own checkout: work is moving, nothing is delivered."""

    @classmethod
    def found(cls, source: ProgressSource) -> Iterator[Self]:
        return (cls(at=edited) for edited in source.edited_at)

    @property
    def state(self) -> str:
        return f"uncommitted edits at {RecordedEventTime(self.at).clock()}, no commit"


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
        for refused in self.wording_refusal(self.text):
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
        """The writer's check: it may print a note, which a decoded record never does."""
        cls.flag(text)
        return next(iter(cls.wording_refusal(text)), None)

    @classmethod
    def wording_refusal(cls, text: str) -> tuple[str, ...]:
        """The refusal this text earns, or none: what a decoded record checks, silently."""
        found = cls.found(text)
        if len(text.split()) < cls.least_words or cls.refuses(found):
            return (cls.refusal.format(cls.least_words, found),)
        return ()

    @staticmethod
    def refuses(found: list[str]) -> bool:
        return bool(found)

    @classmethod
    def flag(cls, text: str) -> None:
        """A family that refuses has no note to print; one that only flags overrides this."""

    @classmethod
    def found(cls, text: str) -> list[str]:
        return sorted({m.group(0).lower() for m in cls.REFUSED.finditer(cls.CITED.sub(' ', text))})


class DirectiveWords(OrderWords):
    """A wording check every text sent to units passes; each check declares itself one."""


class PlainText(DirectiveWords):
    """Prose in the words the user uses: no word that hides what happened."""

    REFUSED = re.compile(r'\b(custody|admission|admit\w*|receipt\w*|checkpoint\w*|provenance|authority|authorities|witness\w*|lattice|surface|disposition\w*|invariant\w*|semantic\w*|ownership|lifecycle|acceptance|canonical|retained|borrow(?:ed|er|ers)?|bounded|certif\w*|recertif\w*|candidate|fence\w*|sidecar\w*|substrate|installed-path|source[- ]paired|exact[- ]head|integration owner|separately owned|follow-?up|remaining scope|not claimed|remains held|load[- ]bearing|cutover|carr(?:y|ies) across|declared dependency|qualifying)\b', re.I)
    refusal = "Jargon {1} hides what happened; say it plainly."

    @staticmethod
    def refuses(found: list[str]) -> bool:
        """Flags, never blocks: a wording slip costs a note, not a resend."""
        return False

    @classmethod
    def flag(cls, text: str) -> None:
        """The note goes to whoever writes the text, at the write; decoding a record never prints it."""
        if found := cls.found(text):
            print(f"Flag: jargon {', '.join(found)} hides what happened; say it plainly.", file=sys.stderr)


class VerdictReason(PlainText):
    """Why a reviewer decided a head, in words that stand on the public record."""

    refusal = "Say why in {}+ words; jargon {} hides what happened."
    least_words = 6


class GrowthReason(PlainText):
    """Why each growing source file needs its new lines, naming every one of them."""

    refusal = "Say why the source grows in {}+ words; jargon {} hides what happened."
    least_words = 12

    @classmethod
    def every(cls, texts: tuple[str, ...]) -> GrowthReason:
        """One reason per repeated --growth, each held to the word minimum, on one record."""
        return cls("; ".join(cls(text).text for text in texts))

    def names(self, path: str) -> bool:
        """One whole word of the reason is the file's path or its file name."""
        words = {word.strip(".,;:()`'\"") for word in self.text.split()}
        return bool({path, Path(path).name} & words)


class OrderCorrectionReason(PlainText):
    """Why the recorded check or run was wrong, in enough words to stand on the record."""

    refusal = (
        "Say what was wrong with the recorded order in {}+ words; jargon {} hides what happened."
    )
    least_words = 8


class StrikeReason(VerdictReason):
    """What a unit did, plainly, in enough words to stand on the public record. A unit's
    wording is answered by a correction, never a strike: a reason whose every quoted span
    is jargon is about wording alone."""

    refusal = "Say what the unit did plainly in {}+ words; jargon {} hides what happened."
    least_words = 10

    @classmethod
    def wording_refusal(cls, text: str) -> tuple[str, ...]:
        cited = [span.group(0).strip('`"“” ') for span in cls.CITED.finditer(text)]
        if cited and all(PlainText.found(span) for span in cited):
            return (f"{cited} is the unit's wording; send it a correction, not a strike.",)
        return super().wording_refusal(text)


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


class DeliveryReport(BluntText):
    """A unit's delivery names the slop it killed or the broken thing it fixed, in the owner's
    voice; a status memo with no verdict in it is half a report."""

    refusal: ClassVar[str] = (
        "Flat report refused: name the slop you killed or the broken thing you fixed, with "
        "file:line, in the owner's voice (blunt, colorful, deadpan, and funny: land one joke on the code). A report with no opinion "
        "in it is half a report."
    )


class FairText(DirectiveWords):
    """The OOPSLA paper proves multiple parents derive every required pair with nothing outside, which composition and
    forwarding supply outside (Fig 3, Thm 4.6): faulting inheritance itself is dogma, not a restated fact.
    Only explanatory prose is checked; code and banned-action fields are not prose."""

    REFUSED = re.compile(r"composition over inheritance|(prefer|favou?r|use) composition|inherits (two|both|multiple|several|\d+)\b"
                         r"|multiple (inheritance|parents) (is (bad|wrong|dangerous|confusing|fragile|slop)|causes|creates|smells|complicates)"
                         r"|diamond problem|fragile base|(avoid|no|stop using|drop) (inheritance|mixins?|multiple (parents|inheritance))"
                         r"|too many (parents|base classes|mixins)|deep (inheritance|hierarch)|mixing (of )?\w+ and \w+"
                         r"|inheritance (abuse|soup|hell|is wrong)", re.I)
    refusal = ("{1} faults inheritance itself. Cite a refactor-audit pattern ID, "
               "name the restated fact and the declaration that derives it.")


class HierarchyText(DirectiveWords):
    """A forwarder restates an implementation pair: explanatory prose must fix the
    hierarchy rather than preserve a forwarding route."""

    REFUSED = re.compile(r"(leave|keep|retain|preserve)[^.\n]{0,60}\bforward|forwarders? (stay|remain)|reach(es|ing)? through", re.I)
    refusal = ("{1} keeps a forwarder or bans fixing the hierarchy: fold the false owner into the real one or compose "
               "providers as parents, and delete every route.")


class DirectiveText:
    """What the commander and the owner's voice send units: order send, order say and the
    connector's send refuse a text failing any of these."""

    @staticmethod
    def checks() -> tuple[type[DirectiveWords], ...]:
        return tuple(DirectiveWords.__subclasses__())

    @classmethod
    def refused(cls, text: str) -> tuple[str, ...]:
        return tuple(refused for family in cls.checks() if (refused := family.refused(text)))

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
class ProblemOrder(LifecycleCapability, DeclaredFamily, StorySource, affix="ProblemOrder"):
    """A problem's order state; an unassigned finding is not a fabricated order."""

    @property
    def line(self) -> str:
        return "No order"

    def check_base(self, repo: Path) -> str:
        raise TodoError("This problem has no active checked order.")

    def occupies_holder(self) -> bool:
        """Whether this order still has work for the unit holding it."""
        return True

    def corrected(self, correction: CorrectOrderTodo) -> ProblemOrder:
        raise TodoError("Only an active checked order takes a correction.")

    finding: Annotated[ProblemFinding, WireOmitDefault()] = field(
        default_factory=UnlocatedProblemFinding
    )

    @property
    def declarations(self) -> tuple[CheckedProblemOrder, ...]:
        return ()

    @property
    def linked_requests(self) -> tuple[PullRequestLink, ...]:
        """The pull requests this order's work holds; an unchecked order holds none."""
        return ()

    def closed_by_merge(
        self, todo: Todo, pulls: ProgressRead, checkout_base: Path
    ) -> tuple[Todo, ...]:
        return ()

    def reissued(self, at: float) -> PendingProblemOrder:
        """This order sent again at ``at`` to a new owner, its first-call window kept."""
        raise TodoError("Only an undelivered order can change owner.")

    def checked(self) -> CheckedProblemOrder:
        raise TodoError("This problem has no checked order.")

    @property
    def phase(self) -> type[OrderPhase]:
        return UnplannedPhase

    def for_phase(self, phase: type[OrderPhase]) -> ProblemOrder:
        raise TodoError("Only a checked order can declare a plan phase.")

    @property
    def accepted(self) -> bool:
        return False

    def require_transfer(self, todo: Todo) -> None:
        raise TodoError("This problem cannot receive another order.")

    def require_reassignment(self, todo: Todo, proposed: Assignment) -> None:
        """Every reassignment is a transfer unless the order state says otherwise."""
        self.require_transfer(todo)

    def require_close(self):
        raise TodoError("Closing requires recorded installed application evidence.")

    def require_installation(self) -> None:
        raise TodoError("Check the delivered order before recording installation.")

    def require_withdrawal(self) -> None:
        raise TodoError("Only an undelivered order can be withdrawn.")

    def withdrawals(self, todo: Todo) -> tuple[str, ...]:
        return ()

    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return ()

    def commander_claims(self) -> tuple[OrderClaim, ...]:
        return ()

    def owed_claims(self) -> tuple[OrderClaim, ...]:
        """Claims that still await the commander's verdict."""
        return ()

    def closing_artifact(self) -> str:
        raise TodoError("Only a delivered order has closing evidence.")

    def orphan_clearing(self, todo: Todo) -> str:
        """What clears this order once its holder is no longer registered."""
        return (f"no live unit can deliver it; send it to a live unit with `order send UNIT "
                f"ORDER.json` naming {todo.id}, or retire it with `order withdraw {todo.id}`")

    def linked(self, order: PendingProblemOrder) -> PendingProblemOrder:
        raise TodoError("This problem already has an order; drop it before linking another.")

    def folded(self, into: str, why: str) -> ProblemOrder:
        if self.items_with(Pending, self):
            raise TodoError("The problem has open orders; link or drop them first.")
        return FoldedProblemOrder(original=self, into=into, why=why)

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
            raise TodoError("scope.shape.parents must name at least one parent class declaration")

    def require_references(self, root: Path, revision: SourceRevision) -> None:
        for location in (*self.parents, *self.members):
            location.require_class(root, revision)


class DeliverySource(DeclaredFamily, affix="DeliverySource"):
    """The command's decoded source, before a commit reference is resolved in its repository."""

    @abstractmethod
    def delivery(self, todo: Todo, repo: Path, evidence: DeliveryEvidence) -> CheckedDelivery: ...

    def require_report(self) -> None:
        raise TodoError("Triage returns --seen REPORT, not a commit or operation.")


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
    def delivery(self, todo: Todo, repo: Path, source: DeliverySource,
                 screenshot: str, seen: str) -> Delivery: ...

    @abstractmethod
    def completed(self, todo: Todo, delivery: Delivery) -> Todo: ...


class TriageScope(OrderScope[ReportDelivery]):
    def delivery(self, todo: Todo, repo: Path, source: DeliverySource,
                 screenshot: str, seen: str) -> ReportDelivery:
        source.require_report()
        if screenshot:
            raise TodoError("Triage returns --seen REPORT, not a screenshot.")
        if not seen:
            raise TodoError("A triage order needs --seen REPORT.")
        return ReportDelivery(report=Path(seen).read_text())

    def instructions(self, problem: str) -> tuple[str, ...]:
        return ("Scope: triage; classify and report only. Do not edit source or deliver commits.",
                "Return the report to your commander; do not open an implementation PR.")

    def verified_claims(
        self, delivery: ReportDelivery, claims: tuple[ProgressClaim, ...],
        comms: Comms, checkout: Path,
    ) -> dict[ProgressClaim, tuple[str, ...]]:
        if claims:
            raise TodoError(
                "scope triage returns a report, not merged, installed or working claims")
        return {}

    def completed(self, todo: Todo, delivery: ReportDelivery) -> Todo:
        order = todo.order.checked().copy_as(ReportedProblemOrder, delivery=delivery)
        return DoneTodoState.closed(todo, order)


@dataclass(frozen=True)
class FixScope(OrderScope[CheckedDelivery]):
    shape: FixShape

    def delivery(self, todo: Todo, repo: Path, source: DeliverySource,
                 screenshot: str, seen: str) -> CheckedDelivery:
        path = seen or screenshot
        evidence = FileDeliveryEvidence(path) if path else NoDeliveryEvidence()
        return source.delivery(todo, repo, evidence)

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
    def linked_requests(self) -> tuple[PullRequestLink, ...]:
        return (*self.pull_requests, *(claim.pull_request for claim in self.commander_claims()))

    def require_linkable(self) -> None:
        """An order still taking work accepts another pull request."""

    def linked_to(self, link: PullRequestLink) -> Self:
        self.require_linkable()
        return replace(self, pull_requests=(*self.pull_requests, link))

    @property
    def installation_commits(self) -> tuple[str, ...]:
        return ()

    @property
    def installation_build(self) -> BuildIdentity:
        return RecordedBuildIdentity.installed()

    def installation(self) -> CommitInstallation:
        return self.installation_build.installation(
            self.installation_commits, self.check.directory
        )

    def require_installation(self) -> None:
        self.installation().require()

    def claimed(self, claim: OrderClaim) -> ClaimedProblemOrder:
        raise TodoError("Only a pending order can be claimed by its holder.")

    def under_review(self, record: OrderHeadReviewRecord) -> ReviewingProblemOrder:
        raise TodoError("Only a claimed order goes under review.")

    def sent_back(self, reason: StrikeReason) -> CheckedProblemOrder:
        return self

    def on_files(self, locations: tuple[FileLine, ...], root: Path) -> bool:
        return (any(self.owner.same_file(location, root) for location in locations)
                or self.finding.on_files(locations, root))

    def checked(self) -> CheckedProblemOrder:
        return self

    @cached_property
    def cited_revision(self) -> SourceRevision:
        return CitedRevisions((*map(PullRequestHeadRevision, self.linked_requests), MainRevision()))

    def require_references(self) -> None:
        root, revision = self.check.directory, self.cited_revision
        self.owner.require_exists(root, revision)
        self.finding.require_references(root, revision)
        self.scope.require_references(root, revision)

    def check_refusals(self, worktrees: Path) -> tuple[str, ...]:
        """Send and correction use the check owner's clean-main acceptance rule."""
        try:
            with MalformedRecord.refusing('order.check'):
                self.require_writable()
                if self.check.directory.is_dir():
                    self.require_check_acceptance(self.check, worktrees)
        except MalformedRecord as error:
            return (f'order.check: {error}; fix: supply a focused measurable command',)
        return ()

    def require_check_acceptance(self, check: OrderCheck, worktrees: Path) -> None:
        repo = check.directory
        Repo.git(repo, 'fetch', '-q', 'origin', 'main')
        check.require_acceptance(self.check_base(repo), worktrees)

    def source_refusals(self, context: CommanderContext, document: OrderFile,
                        traced: FileLine | NoSourceReference) -> list[str]:
        """Scope, prose and pattern citations share the order's source revision."""
        check = self.check
        problems = []
        for label, validate in (('order.source', self.require_references),
                                ('order.check.directory', check.require_directory)):
            try:
                with MalformedRecord.refusing(label):
                    validate()
            except MalformedRecord as error:
                problems.append(f'{label}: {error}; fix: name an existing source location or directory')
        references = tuple(dict.fromkeys(
            reference for reference in (
                *FileLine.scan('\n'.join((document.prose, *self.edits))),
                *(reference for slop in document.slop for reference in (slop.at, *slop.by.cited())),
                *traced.located(),
            ) if reference not in self.created
        ))
        deleted = {Path(module).stem for module in self.deletes.modules}
        if deleted and (unread := context.importers(deleted, check)):
            problems.append(f'the order deletes {sorted(deleted)}, which {unread} import; '
                            'the check must also count their `from agent_comms` imports')
        for reference in references:
            try:
                with MalformedRecord.refusing(reference.to_wire()):
                    reference.require_exists(check.directory, self.cited_revision)
            except MalformedRecord as error:
                problems.append(f'{error}; fix: cite an existing source line')
        body = '\n'.join((document.prose, *self.edits, self.report))
        if any((check.directory / reference.file).resolve().is_relative_to(
                   check.directory / 'tools/') for reference in references) and not all(
                said in body for said in ('--root tools', '--root src')):
            problems.append('the order changes tools/: say "ratchet with --root tools and with '
                            '--root src, each as its own command"')
        return problems

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
    corrections: Annotated[tuple[CorrectOrderTodo, ...], WireOmitDefault()] = ()
    run: OrderRun
    deletes: Annotated[OrderDeletion, WireOmitDefault()] = field(
        default_factory=NoOrderDeletion
    )
    serves: Annotated[type[OrderPhase], WireOmitDefault()] = UnplannedPhase
    created: Annotated[tuple[FileLine, ...], WireOmitDefault()] = ()

    def holds_head(self, request: PullRequestHead) -> bool:
        return PullRequestLink(request.repo, request.pr) in self.linked_requests

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
            raise TodoError("\n".join(problems))

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

    def closed_by_merge(
        self, todo: Todo, pulls: ProgressRead, checkout_base: Path
    ) -> tuple[Todo, ...]:
        repo = todo.repository
        source = PullRequestSource(todo, pulls)
        return tuple(self.run.after_merge(todo, merged)
                     for merged in self.observed_merges(repo, source))

    def observed_merges(
        self, repo: Repo, source: PullRequestSource
    ) -> tuple[MergedProblemOrder, ...]:
        return tuple(self.merged(merged.sha) for merged in MergedProgress.found(source)
                     if repo.contains(merged.sha))

    def open_pull_request(self, todo: Todo, checkout: Path, title: str, body: str,
                          store: TodoStore, actor: ThreadIncarnation) -> str:
        """Create once; record the returned identity on this order, not in its prose."""
        todo.require_actor(actor, todo.held().generation)
        self.require_linkable()
        created = subprocess.run(
            ("gh", "pr", "create", "--draft", "-R", todo.repo.to_wire(),
             "--title", title, "--body-file", "-"),
            input=body,
            cwd=checkout,
            capture_output=True, text=True, timeout=60)
        if created.returncode:
            raise TodoError(created.stderr.strip() or "Order pull request creation failed.")
        url = created.stdout.strip()
        try:
            store.apply(todo.id, RecordPullRequest(todo.revision, actor,
                                                  PullRequestLink.created(todo.repo, url),
                                                  todo.held().generation))
        except (ValueError, TodoError, subprocess.SubprocessError) as error:
            raise TodoError(f"Created {url}, but recording its link failed: {error}. "
                             "Use order pr --existing; do not create another PR.") from error
        return url

    def work_instructions(self) -> tuple[str, ...]:
        return (f"Owner: {self.owner.to_wire()}",
                f"Check: {self.check.directory} :: {self.check.command}",
                *self.deletes.instructions(), *self.run.instructions(),
                *(f"{n}. {edit}" for n, edit in enumerate(self.edits, 1)),
                "Banned: " + "; ".join(self.banned), "Report: " + self.report,
                CommanderPackage.voice().read_text(encoding="utf-8").strip(),
                *(audience.order_line(", ".join(rule.name for rule in rules))
                  for audience, rules in RuleAudience.grouped(RuleFile.packaged())))

    def instructions(self, todo):
        return "\n".join((f"Problem: {todo.id}: {todo.text}", *self.finding.instructions(),
            f"Order: {self.id}", *self.scope.instructions(todo.id),
            *self.work_instructions()))

    def delivered(self, delivery: CheckedDelivery):
        delivery.at.require_delivery()
        return self.copy_as(DeliveredProblemOrder, delivery=delivery)

    def installed(self, works: str, artifact: str) -> ProblemOrder:
        raise TodoError("Check the delivered order before recording installation.")

    def require_delivery(self, order_id: str) -> None:
        raise TodoError("Only an open order can be delivered.")

    def require_checked(self, problem: str) -> None:
        """An installed claim stands on this order's recorded check result."""
        raise TodoError(f"An installed claim needs the recorded check result of {problem}; "
                         "its order has none.")

    def withdrawn(self, previous: Assignment, why: str, superseded_by: Successor) -> ProblemOrder:
        self.require_withdrawal()
        return self.copy_as(WithdrawnProblemOrder, previous=previous, why=why,
                            superseded_by=superseded_by)


@dataclass(frozen=True, kw_only=True)
class OpenProblemOrder(CheckedProblemOrder, declared_name=None):
    """An active order keeps corrections and accepts its checked delivery by identity."""

    def corrected(self, correction: CorrectOrderTodo) -> Self:
        return correction.corrected(self)

    def require_delivery(self, order_id: str) -> None:
        if self.id != order_id:
            raise TodoError("The order changed; refresh its state before delivering it.")


@dataclass(frozen=True, kw_only=True)
class AwaitingInstallationProblemOrder(OpenProblemOrder, declared_name=None):
    def waiting_on_install(self, todo: Todo, read: dict[str, OrderProgress]) -> tuple[Todo, ...]:
        return (todo,)

    def occupies_holder(self) -> bool:
        return False

    def orphan_clearing(self, todo: Todo) -> str:
        return (f"its work is done and waits on install; once installed, close it with "
                f"`order close {todo.id} --works WHAT-RAN --artifact FILE`")


@dataclass(frozen=True, kw_only=True)
class MergedProblemOrder(AwaitingInstallationProblemOrder):
    """The checked order installed by a merge, with its actual merge identity."""
    sha: str

    def check_base(self, repo: Path) -> str:
        return f'{self.sha}^1'

    @property
    def line(self) -> str:
        return self.run.merged_line

    def closed_in(self, todo: Todo) -> Todo:
        return DoneTodoState.closed(todo, self)

    def require_linkable(self) -> None:
        raise TodoError(f"Order {self.id} merged as {self.sha[:9]}; its pull requests are closed. "
                         "Ask the commander for a send-back before opening or linking another PR.")

    def waiting_in(self, todo: Todo) -> Todo:
        return (todo if todo.order == self
                else replace(todo, order=self, revision=todo.revision + 1))

    def observed_merges(
        self, repo: Repo, source: PullRequestSource
    ) -> tuple[MergedProblemOrder, ...]:
        return (self,)

    def require_close(self) -> None:
        self.run.require_merge_close()

    @property
    def installation_commits(self) -> tuple[str, ...]:
        return (self.sha,)

    def installed(self, works: str, artifact: str) -> ProblemOrder:
        self.run.require_closing(works)
        return self.copy_as(InstalledMergedProblemOrder, works=works, artifact=artifact)

    def closing_artifact(self) -> str:
        raise TodoError("A merged order records no delivered run; pass --artifact FILE "
                         "showing the run on the installed build.")


@dataclass(frozen=True, kw_only=True)
class DeliverableProblemOrder(FreshMainCheck, OpenProblemOrder, declared_name=None):
    """Pending work and a holder's claim both await the commander's delivery verdict."""

    def sent_back(self, reason: StrikeReason) -> PendingProblemOrder:
        return self.fresh_order(time.time(), (*self.edits, reason.text))

    def fresh_order(self, at: float, edits: tuple[str, ...]) -> PendingProblemOrder:
        window = (self.first_call_deadline.recorded_at("A first-call deadline")
                  - self.at.recorded_at("An order"))
        return self.copy_as(
            PendingProblemOrder, id=uuid4().hex[:12], at=RecordedEventTime(at),
            first_call_deadline=RecordedEventTime(at + window), edits=edits,
        )

    def closed_by_merge(
        self, todo: Todo, pulls: ProgressRead, checkout_base: Path
    ) -> tuple[Todo, ...]:
        """An undelivered order whose check already passes on fresh main is already fixed:
        it merges at that main commit, never reissued.
        """
        merged = super().closed_by_merge(todo, pulls, checkout_base)
        if merged:
            return merged
        repo = self.check.directory
        main = Repo.git(
            repo, "rev-parse", f"{FreshMainCheck.check_base(self, repo)}^{{commit}}"
        ).strip()
        ran = self.check.at(main, self.check.directory, checkout_base)
        if not self.check.result.distance(ran).project(lambda away: away == 0, lambda _: False):
            return ()
        return (self.run.after_merge(todo, self.merged(main)),)


@dataclass(frozen=True, kw_only=True)
class PendingProblemOrder(Pending, DeliverableProblemOrder):
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    at: EventTime = field(default_factory=lambda: RecordedEventTime(time.time()))
    first_call_deadline: EventTime = field(
        default_factory=lambda: RecordedEventTime(time.time() + 120))

    def assignment_refusals(self, context: CommanderContext, document: OrderFile, unit: str,
                            group: Sequence[str], listed: Mapping[str, Todo],
                            target: OrderedProblems,
                            traced: FileLine | NoSourceReference) -> list[str]:
        """The document's selected problems, phase and existing owners must agree."""
        reading = context.plan_reading(
            context.comms.goals.todos.history_for_commander(context.incarnation))
        problems = list(self.phase.order_refusals(reading, document.unplanned_reason))
        held = tuple(todo for todo in listed.values()
                     if todo.holder_state.current_assignments() and todo.order.occupies_holder())
        holder = context.comms.registry.require(unit).incarnation
        busy = tuple(todo for todo in held
                     if todo.held().owner_identity == holder and todo.id not in group)
        if busy:
            snapshot = context.comms.registry.snapshot()
            occupied = {thread.name for todo in held
                        for thread in todo.held().holder_in(snapshot)} | {context.commander}
            idle = sorted(
                candidate.name
                for thread in snapshot.active_threads(snapshot.active_candidates())
                if thread.name not in occupied
                for candidate in thread.execution.order_candidates(thread)
            )
            problems.append(f'{unit} already holds orders '
                            f'{", ".join(todo.id for todo in busy)}; '
                            f'running units holding no order: {", ".join(idle) or "none"}')
        if not group or any(not ProblemNumber.accepts(pid) for pid in group):
            problems.append('problems: fix: supply an array of P<n> IDs, or use order problem')
        problems.extend(target.refusals(document, group, listed))
        for pid in group:
            try:
                with MalformedRecord.refusing(pid):
                    if pid in listed:
                        listed[pid].holder_state.require_trace(unit, traced)
            except MalformedRecord as error:
                problems.append(str(error))
        return problems

    def require_send(self, context: CommanderContext, document: OrderFile, unit: str,
                     group: Sequence[str], listed: Mapping[str, Todo], target: OrderedProblems,
                     reassign: Reassignment, holders: Sequence[str], clean: bool,
                     traced: FileLine | NoSourceReference) -> None:
        """Collect field refusals before recording any part of this document."""
        problems = document.problems_found(clean)
        try:
            context.comms.registry.storage_rule.require_quota(context.comms.registry.require(unit))
        except SelectedToolDenied as quota:
            problems.append(f'{unit}: {quota}: it cleans up its own checkouts and scratch '
                            'before it gets new work')
        problems.extend(self.assignment_refusals(
            context, document, unit, group, listed, target, traced))
        problems.extend(self.source_refusals(context, document, traced))
        problems.extend(self.check_refusals(context.comms.registry.storage_rule.checkout_base))
        problems.extend(reassign.refusals(holders))
        if problems:
            sys.exit('Refused order:\n  ' + '\n  '.join(problems))
        fired = tuple(listed[pid] for pid in group if pid in listed and listed[pid].struck_out(unit))
        self.run.require_assignment(context.comms.registry.require(unit).incarnation,
                                    fired, ProblemGoalOrder.StrikeLimit)

    def message(self, context: CommanderContext, document: OrderFile, unit: str, group: Sequence[str],
                replacing: bool, reassign: Reassignment) -> str:
        """Render owned fields; never parse this message back into an order."""
        instructions = '\n'.join((document.prose, *(slop.render() for slop in document.slop),
            f'Problems: {", ".join(group)}', *self.work_instructions()))
        replacement = '\nThis replaces my earlier order on it; drop that one.' if replacing else ''
        storage = context.comms.registry.storage_rule.order_text(
            context.comms.registry.require(unit),
            [f'{repo.checkout}/.venv/bin/python' for repo in context.repos])
        return (instructions + replacement + reassign.note() + '\n' + storage
                + '\nIf two lines of this order conflict, take the narrower reading, '
                'say which one you took, and proceed; do not stop to ask.'
                + '\nStart now and push as you go.\n'
                + '\n'.join(line for pid in group for line in self.scope.instructions(pid)))

    def send(
        self, context: Commander, document: OrderFile, unit: str, clean: bool,
        reassign: Reassignment, target: OrderedProblems,
        traced: FileLine | NoSourceReference, answers: tuple[MessageReference, ...],
        request: RequestSelection,
    ) -> None:
        """Validate the complete issuance before recording, then publish its owned effects."""
        group = target.group(document)
        listed = {todo.id: todo for todo in context.todos()}
        target.carried(self, group, listed).issue(
            context, document, unit, clean, reassign, target, traced, answers, request,
            group, listed)

    def carrying(self, links: Iterable[PullRequestLink]) -> Self:
        """This order holding the problem's pull requests too, so its cites stand on their heads."""
        carried = tuple(link for link in dict.fromkeys(links) if link not in self.pull_requests)
        return replace(self, pull_requests=(*self.pull_requests, *carried)) if carried else self

    def issue(
        self, context: Commander, document: OrderFile, unit: str, clean: bool,
        reassign: Reassignment, target: OrderedProblems,
        traced: FileLine | NoSourceReference, answers: tuple[MessageReference, ...],
        request: RequestSelection, group: Sequence[str], listed: Mapping[str, Todo],
    ) -> None:
        holder = context.comms.registry.require(unit).incarnation
        holders = sorted({assignment.owner for pid in group if pid in listed
                          for assignment in listed[pid].holder_state.current_assignments()
                          if assignment.owner_identity != holder})
        replacing = target.replacing(holder, group, listed)
        self.require_send(context, document, unit, group, listed, target,
                          reassign, holders, clean, traced)
        struck_already = replacing and listed[group[0]].struck_on_order(unit)
        measured = MeasuredReview.read(context.comms.goals.todos)
        review_at = measured.review_at(self)
        message = self.message(context, document, unit, group, replacing, reassign)
        request_id = request.for_order(context.comms.goals.todos, context.incarnation)
        target.record(context, self, unit, group, traced, request_id, review_at)
        self.publish(context, unit, group, message, answers)
        self.notify_replaced(context, unit, group, holders, listed, reassign)
        print(f'{measured.summary}; review timer {review_at.clock()}')
        print(self.id)
        if replacing and not clean:
            reassign.send_back(context, document, unit, group[0], struck_already)

    def publish(
        self, context: Commander, unit: str, group: Sequence[str], message: str,
        answers: tuple[MessageReference, ...],
    ) -> None:
        """Bind the one published message to every problem through its native command."""
        sent = context.post(VerdictAudience((unit,)), message, answers)
        for pid in group:
            context.comms.goals.change_problem(pid, OrderMessage(
                context.comms.goals.todos.get(pid).revision, context.actor_identity, sent,
            ), context.commander)

    def notify_replaced(
        self, context: Commander, unit: str, group: Sequence[str], holders: Sequence[str],
        listed: Mapping[str, Todo], reassign: Reassignment,
    ) -> None:
        """The order that takes over the work tells each former holder why."""
        for previous in holders:
            context.post(VerdictAudience((previous,)),
                         f'Taken off you: {", ".join(group)} ("{listed[group[0]].text[:120]}") '
                         f'now belongs to {unit} because: {reassign.because()}. '
                         'Stop that work; do not touch its files.')

    def reissued(self, at: float) -> PendingProblemOrder:
        return self.fresh_order(at, self.edits)

    def claimed(self, claim: OrderClaim) -> ClaimedProblemOrder:
        return self.copy_as(ClaimedProblemOrder, claim=claim)

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

    def original(self, source: BusReader) -> Message:
        return Message.from_message(source.lookup_message(self.message.message_id).message)

    @staticmethod
    def originals[K](
        source: BusReader, claims: Mapping[K, tuple[OrderClaim, ...]]
    ) -> dict[K, tuple[Message, ...]]:
        """Every claim's original from the caller's acquired bus snapshot."""
        if not any(claims.values()):
            return {}
        return {key: tuple(claim.original(source) for claim in held)
                for key, held in claims.items()}

    @classmethod
    def for_order(
        cls, todo: Todo, actor: ThreadIncarnation, number: int, head: str,
        report: str, answer: MessageReference, *, store: TodoStore,
    ) -> OrderClaim:
        request = store.owner_request(todo.request.require())
        return cls(
            PullRequestLink(todo.repo, number), head,
            Message(actor.name, request.commander.name, report, InfoKind, answers=(answer,)),
        )


@dataclass(frozen=True, kw_only=True)
class ClaimedProblemOrder(BranchPointCheck, DeliverableProblemOrder):
    claim: OrderClaim

    @property
    def line(self) -> str:
        return "Claimed by the unit, waiting on the commander's verdict"

    @property
    def work_commit(self) -> str:
        return self.claim.head

    def check_base(self, repo: Path) -> str:
        Repo.git(repo, "fetch", "-q", "origin", f"refs/pull/{self.claim.pull_request.number}/head")
        return super().check_base(repo)

    def require_reassignment(self, todo: Todo, proposed: Assignment) -> None:
        """A corrected order to the claimant replaces its claim; another owner is a transfer."""
        if proposed.owner_identity != todo.holder_state.previous_assignment().owner_identity:
            self.require_transfer(todo)

    def commander_claims(self) -> tuple[OrderClaim, ...]:
        return (self.claim,)

    def closed_by_merge(
        self, todo: Todo, pulls: ProgressRead, checkout_base: Path
    ) -> tuple[Todo, ...]:
        merged = super().closed_by_merge(todo, pulls, checkout_base)
        if merged:
            return merged
        if any(
            request.number == self.claim.pull_request.number
            for request in pulls.listed(todo.repo, ClosedListedPullRequest, todo)
        ):
            return (
                replace(
                    todo, order=self.fresh_order(time.time(), self.edits),
                    revision=todo.revision + 1,
                ),
            )
        return ()

    def owed_claims(self) -> tuple[OrderClaim, ...]:
        return (self.claim,)

    def under_review(self, record: OrderHeadReviewRecord) -> ReviewingProblemOrder:
        """The reviewed head is GitHub's current head (HeadVerdict.reviewed); the claim's head
        is what the unit said at claim time and never gates a later push."""
        return self.copy_as(ReviewingProblemOrder, review=record)


@dataclass(frozen=True, kw_only=True)
class ReviewingProblemOrder(ClaimedProblemOrder):
    """A claim a named reviewer is reading at its exact head; nothing is owed until the verdict."""

    review: OrderHeadReviewRecord

    @property
    def line(self) -> str:
        return (f"Under review by {self.review.actor.name} on #{self.review.request.pr} "
                f"at {self.review.request.head[:9]}")

    @property
    def work_commit(self) -> str:
        return self.review.request.head

    def owed_claims(self) -> tuple[OrderClaim, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class DeliveredProblemOrder(AwaitingInstallationProblemOrder):
    delivery: OrderDelivery

    def check_base(self, repo: Path) -> str:
        return self.delivery.check_base(repo)

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
        return replace(self, delivery=self.delivery.reproved(delivery))

    def observed_merges(
        self, repo: Repo, source: PullRequestSource
    ) -> tuple[MergedProblemOrder, ...]:
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

    def observed_merges(
        self, repo: Repo, source: PullRequestSource
    ) -> tuple[MergedProblemOrder, ...]:
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
            raise TodoError("Describe what worked and name its artifact file.")

    def installed(self, works: str, artifact: str) -> Self:
        if (works, artifact) != (self.works, self.artifact):
            raise TodoError("The order already records different installed evidence.")
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
            raise TodoError("Describe the inspected code and verdict in ten or more words.")
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
            raise TodoError("The hunt must name this commander's current order.")


class TodoStoreRows(StoreRows):
    """The tables of TodoStore; each row declares itself one."""


@dataclass(frozen=True)
class HuntRecord(TodoStoreRows, TypedTable):
    id: Annotated[str, Column(primary_key=True)]
    commander: Annotated[ThreadIncarnation, Column(index=True)]
    hunt: Hunt


class SharedWorktreeRefusal(TodoError):
    """Two different order holders cannot write into the same physical worktree."""

    def __init__(self, problem: str, holding_problem: str, holder: ThreadIncarnation,
                 holding_holder: ThreadIncarnation, worktree: Path) -> None:
        self.problem = problem
        self.holding_problem = holding_problem
        self.holder = holder
        self.holding_holder = holding_holder
        self.worktree = worktree
        super().__init__(
            f"{problem} for {holder.name} shares worktree {worktree} with "
            f"{holding_problem} held by {holding_holder.name}."
        )


class TodoStoreUnwritable(TodoError):  # noqa: N818 - domain-specific storage failure
    """The todo store could not complete its write; its original cause is preserved."""


class NotOrderCommander(TodoError):  # noqa: N818 - domain-specific refusal outcome
    """The caller is not the commander who created this order."""

    def __init__(self, current: Todo, actor: ThreadIncarnation) -> None:
        self.current = current
        self.actor = actor
        super().__init__(
            f"{current.id} belongs to commander {current.creator_identity.name}; "
            f"{actor.name} is not its commander."
        )


class TodoHolder(ABC):
    """Current, released and absent holders own their assignment behaviour."""

    def held(self, current: Todo) -> Assignment:
        raise TodoError(f"{current.id} has no open order.")

    @abstractmethod
    def previous_assignment(self) -> Assignment: ...

    def owned_by(self, actor: ThreadIncarnation, generation: GoalId) -> bool:
        return False

    def proposed_assignment(self, proposed: Assignment) -> Assignment:
        return proposed

    @abstractmethod
    def require_trace(self, owner: str, traced: SourceReference) -> None: ...

    def successor(self, proposed: Assignment, current: Todo) -> Assignment:
        return proposed

    def current_assignments(self) -> tuple[Assignment, ...]:
        return ()

    def retained_assignments(self, current: Todo, pulls: PullRequests) -> tuple[Assignment, ...]:
        return ()

    def restored(self, current: Todo) -> Todo:
        """A current or absent holder has no released assignment to restore."""
        return current

    def after_void(self, voided: Todo) -> Todo:
        """A void changes no holder unless the holder state was made by the voided strikes."""
        return voided


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
    generation: GoalId
    previous_owners: Annotated[
        tuple[ThreadIncarnation, ...], WireOmitDefault()
    ] = ()

    @classmethod
    def of(
        cls, owner: ThreadIncarnation, parent: ThreadIncarnation, generation: GoalId
    ) -> Assignment:
        return cls(owner.name, owner.wire_birth, parent.name, parent.wire_birth, generation)

    @classmethod
    def sql_field(cls, name: str) -> str:
        """Queries use the codec's declared wire key, not another record shape."""
        return f"json_extract({{assignment}}, '$.{FieldCodec.record_key(cls, name)}')"

    def __post_init__(self) -> None:
        _text(self.owner, "Thread name", 128)
        _text(self.parent, "Thread name", 128)
        try:
            self.owner_identity.require_recorded()
        except ValueError as error:
            raise TodoError("Assignment requires a recorded owner incarnation") from error

    @property
    def owner_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created)

    def holder_in(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        """The registered thread born as this hold's recorded holder; empty once that thread
        was deleted or registered again under a new birth."""
        return snapshot.threads_for(self.owner_identity.require_recorded())

    def running_holders(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        """A retained assignment follows its live incarnation, never a reused name."""
        return tuple(owner for owner in self.holder_in(snapshot) if owner.process_alive)

    def require_available_in(self, db: sqlite3.Connection, problem: str,
                             assigner: ThreadIncarnation, snapshot: RegistrySnapshot) -> None:
        """Validate this holder against open work using the transaction's registry snapshot."""
        TodoStore.require_unit_in(db, self.owner_identity, assigner)
        worktrees = {owner.project_path for owner in self.holder_in(snapshot)}
        for held in TodoStore._open(db, "assignment IS NOT NULL", ()):
            previous = held.held()
            if held.id == problem or previous.owner_identity == self.owner_identity:
                continue
            for other in previous.holder_in(snapshot):
                if other.project_path in worktrees:
                    raise SharedWorktreeRefusal(
                        problem, held.id, self.owner_identity, other.incarnation, other.project_path
                    )

    @property
    def parent_identity(self) -> ThreadIncarnation:
        """The acting assigner: commander, registered fork, or watcher; not Todo.creator."""
        return ThreadIncarnation.from_wire({"name": self.parent, "created_at": self.parent_created})

    def held(self, current: Todo) -> Assignment:
        return self

    def proposed_assignment(self, proposed: Assignment) -> Assignment:
        return replace(proposed, previous_owners=self.previous_owners)

    def current_assignments(self) -> tuple[Assignment, ...]:
        return (self,)

    def owned_by(self, actor: ThreadIncarnation, generation: GoalId) -> bool:
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


# Todo stores these commands, so it is declared after them; the alias reads it lazily.
type CommandedTodo = Todo


@dataclass(frozen=True)
class TodoCommand(Command[CommandedTodo, CommandedTodo], DeclaredFamily, affix="TodoOperation"):
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

    def validate(self, db: sqlite3.Connection, current: Todo, store: TodoStore) -> None:
        """Validate related records inside the operation's write transaction."""

    def write(self, db: sqlite3.Connection, current: Todo, store: TodoStore) -> Todo:
        """The operation owns its related writes in the Todo transaction."""
        result = self.apply(current)
        if result != current:
            self.validate(db, current, store)
            result.upsert(db)
        return result

    def apply(self, current: Todo) -> Todo:
        self.require_current(current)
        return self.changed(current)

    def require_current(self, current: Todo) -> None:
        current.require_revision(self.expected_revision)

    @abstractmethod
    def changed(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class ProblemCommand:
    """One command addressed to one problem's Todo."""
    problem: str
    operation: TodoCommand

    def write(self, db: sqlite3.Connection, store: TodoStore) -> None:
        """This command inside the caller's Todo transaction, on the problem's current row."""
        self.operation.write(db, store._current(db, self.problem), store)


@dataclass(frozen=True)
class JudgedCall(ProblemCommand):
    """One verdict on one tool call of the order a problem holds."""
    problem: Annotated[str, Description("Problem ID that holds the judged tool call"),
                       DefaultNonNull()]
    operation: Annotated[ToolCallVerdict, Description("Verdict on that tool call"),
                         DefaultNonNull()]


@dataclass
class TodoBatch:
    """Verdicts written together: the Todo-only ones in one transaction, then each one
    that writes beyond the Todo store on its own owner path, in that order."""
    together: list[JudgedCall] = field(default_factory=list)
    alone: list[JudgedCall] = field(default_factory=list)

    def ordered(self) -> tuple[JudgedCall, ...]:
        return (*self.together, *self.alone)

    def record(self, goals: Goals) -> tuple[Todo, ...]:
        with goals.todos.writing() as db:
            for call in self.together:
                call.write(db, goals.todos)
            written = tuple(goals.todos._current(db, call.problem) for call in self.together)
        for call, changed in zip(self.together, written):
            call.operation.after_record(goals, changed)
        return (*written, *(call.operation.record(goals, call.problem) for call in self.alone))


class TodoOperation(TodoCommand, declared_name=None):
    """Commands the ordinary commander's tools accept."""

    def authored_by(self, actor: ThreadIncarnation) -> Self:
        return replace(self, actor=actor)


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    """The order's commander gives the problem to a unit."""

    actor: ThreadIncarnation
    proposed: Assignment
    review_at: EventTime
    order: PendingProblemOrder | MergedProblemOrder
    request: OwnerRequestId
    traced: Annotated[FileLine | NoSourceReference, WireOmitDefault()] = NoSourceReference()

    @classmethod
    def by(cls, actor: ThreadIncarnation, expected_revision: int, owner: ThreadIncarnation,
           generation: GoalId, review_at: EventTime,
           order: PendingProblemOrder | MergedProblemOrder,
           request: OwnerRequestId, traced: FileLine | NoSourceReference) -> AssignTodo:
        """The one place the typed actor and the assignment's recorded assigner are made."""
        return cls(expected_revision, actor, Assignment.of(owner, actor, generation),
                   review_at, order, request, traced)

    def authored_by(self, actor: ThreadIncarnation) -> Self:
        return replace(self, actor=actor, proposed=replace(self.proposed,
                       parent=actor.name, parent_created=actor.wire_birth))

    def validate(self, db: sqlite3.Connection, current: Todo, store: TodoStore) -> None:
        self.proposed.require_available_in(
            db, current.id, self.actor,
            RegistryStore.at(store.root).read_snapshot_unlocked())
        OwnerRequest.require(db, id=self.request).require_open(current.creator_identity)

    def apply(self, current: Todo) -> Todo:
        self.review_at.require_recorded("A review time")
        proposed = current.proposed_assignment(self.proposed)
        if (current.assignment, current.review_at, current.order, current.request) == (
                proposed, self.review_at, self.order, self.request):
            return current
        return super(AssignTodo, replace(self, proposed=proposed)).apply(current)

    def changed(self, current: Todo) -> Todo:
        self.actor.require_may_change(current)
        if self.order != current.order:
            self.order.require_wording()
            self.order.require_writable()
        assigned = (current if current.assignment == self.proposed
                    else current.state.assign(current, self.proposed, self.traced))
        return replace(assigned, review_at=self.review_at,
                       order=self.order, prior_orders=current.orders_before(self.order),
                       request=self.request,
                       revision=current.revision + 1)


@dataclass(frozen=True)
class CitedEvidence:
    evidence: Annotated[tuple[FileLine, ...], WireOmitDefault()] = field(
        default=(), kw_only=True)

    def require_evidence(self, current: Todo) -> None:
        order = current.order.checked()
        self.require_cited(order.check.directory, order.cited_revision)

    def require_cited(self, root: Path, source: SourceText) -> None:
        if not self.evidence:
            raise TodoError("A strike needs a checkable file:line, not a board row or goal text.")
        for cited in self.evidence:
            cited.require_exists(root, source)


@dataclass(frozen=True)
class CitedStrike(CitedEvidence, TodoOperation, declared_name=None):
    def prepare(self, goals: Goals, current: Todo) -> Self:
        self.require_evidence(current)
        return self


@dataclass(frozen=True)
class StrikeTodo(CitedStrike, StoryEvent):
    actor: ThreadIncarnation
    previous: Assignment
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def prepare(self, goals: Goals, current: Todo) -> Self:
        current.state.require_open(current)
        DirectiveText.require(self.reason)
        return super().prepare(goals, current)

    def changed(self, current: Todo) -> Todo:
        self.actor.require_may_change(current)
        current.require_assignment(self.previous)
        _text(self.reason, "Strike reason", 2000)
        StrikeReason(self.reason)
        if any(self.repeats(recorded) for recorded in self.prior_strikes(current)):
            return current
        return self.recorded(
            replace(current, strikes=(*current.strikes, self), revision=current.revision + 1))

    def recorded(self, struck: Todo) -> Todo:
        return self.taken_away(struck)

    def taken_away(self, struck: Todo) -> Todo:
        """The strike that reaches StrikeLimit takes the task away in the same write, through
        the release `order fire` records. The release states its own reason, the limit; the
        strike already carries what the unit did."""
        if not struck.struck_out(self.previous.owner):
            return struck
        limit = StrikeReason(f"{self.previous.owner} took {ProblemGoalOrder.StrikeLimit} strikes "
                             f"on {struck.id}, so the task goes to another unit.")
        return LimitReleaseTodo(struck.revision, self.actor, self.previous, limit, self.at).apply(struck)

    def story_text(self) -> str:
        return f"{self.actor.name} struck {self.previous.owner}: {self.reason}"

    def prior_strikes(self, current: Todo) -> tuple[StrikeTodo, ...]:
        return current.live_strikes(self.previous.owner)

    def matches_failed_check(self, strike: FailedCheckStrike) -> bool:
        return False

    def repeats(self, recorded: StrikeTodo) -> bool:
        return False


@dataclass(frozen=True)
class SendBackStrike(StrikeTodo):
    """One send-back is one miss: the verdicts of one judging call share its recorded moment,
    so every edit after the first repeats that call's strike and the order is sent back once;
    a later call with the same words is a second miss and a second strike."""

    def repeats(self, recorded: StrikeTodo) -> bool:
        return recorded.previous.owner == self.previous.owner and recorded.at == self.at

    def recorded(self, struck: Todo) -> Todo:
        order = struck.order.checked().sent_back(StrikeReason(self.reason))
        return replace(super().recorded(struck), order=order, prior_orders=struck.orders_before(order))


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

    def after_record(self, goals: Goals, changed: Todo) -> None:
        if changed.revision == self.expected_revision:
            return
        count = len(changed.live_strikes(self.previous.owner))
        line = (f"Core strike {count} on {changed.id} order {self.order_id}: "
                f"{self.previous.owner} {self.reason}"
                + (f"; {changed.id} taken away" if not changed.holder else ""))
        goals.bus.publisher.publish_ordinary(
            Message(sender=changed.creator, target=CommsTag.channel(), body=line,
                    type=InfoKind),
            destination=CommandTarget.named(CommsTag.channel()), origin=AgentOrigin())


@dataclass(frozen=True)
class VerdictAudience:
    """The threads a send is for; a send reaching many threads wakes no thread outside it."""
    threads: tuple[str, ...]

    @classmethod
    def of(cls, unit: str, todo: Todo) -> VerdictAudience:
        """The order records its commander; a request lookup adds no authority."""
        return cls(tuple(dict.fromkeys((unit, todo.creator))))

    @property
    def target(self) -> NamedTarget:
        name = self.threads[0] if len(self.threads) == 1 else CommsTag.channel()
        return CommandTarget.named(name)

    def publish(self, goals: Goals, actor: ThreadIncarnation, text: str) -> None:
        target = self.target
        goals.bus.publisher.publish_ordinary(
            Message(sender=actor.name, target=target.name,
                    body=target.addressed(self.threads, text), type=InfoKind),
            destination=target, origin=AgentOrigin(),
        )


@dataclass(frozen=True)
class OrderVerdict(TodoCommand, StoryEvent, declared_name=None):
    """The commander's verdict on one tool call the holder made for its order, kept on the order."""

    def send_back_times(self) -> tuple[float, ...]:
        return ()

    audience: ClassVar[type[VerdictAudience]] = VerdictAudience
    actor: ThreadIncarnation
    previous: Assignment
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()), kw_only=True)

    def story_text(self) -> str:
        return f"{self.actor.name} judged {self.previous.owner}: {self.line}"

    def implied_strikes(self) -> tuple[StrikeTodo, ...]:
        return ()

    def staged(self, batch: TodoBatch, call: JudgedCall) -> None:
        """A Todo-only verdict joins the batch's one write transaction."""
        batch.together.append(call)

    def judged_calls(self) -> tuple[str, ...]:
        """The holder's tool calls this verdict judges by identity."""
        return ()

    @abstractmethod
    def judged(self, current: Todo) -> Todo: ...

    @property
    @abstractmethod
    def line(self) -> str: ...

    def require_current(self, current: Todo) -> None:
        # A verdict's time bounds which edits it judges, so the write refuses one it cannot date.
        self.at.require_recorded("A verdict")
        super().require_current(current)

    def changed(self, current: Todo) -> Todo:
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

    def require_current(self, current: Todo) -> None:
        current.require_commander(self.actor)
        _text(self.tool_call_id, "Judged tool call ID", 200)
        if self.tool_call_id in current.judged_calls:
            raise TodoError(f"Tool call {self.tool_call_id} already has a verdict on {current.id}.")
        super().require_current(current)


@dataclass(frozen=True)
class OrderQuote:
    quote: str
    rule: ClassVar[str] = "Quote 4+ words of the order this verdict carries out."

    def require_quote(self, text: str) -> None:
        quoted = " ".join(self.quote.split())
        if len(quoted.split()) < 4 or quoted not in " ".join(text.split()):
            raise TodoError(self.rule)


@dataclass(frozen=True)
class QuotedVerdict(OrderQuote, OrderVerdict, declared_name=None):
    def story_text(self) -> str:
        return f'{super().story_text()}; quoted: "{self.quote}"'

    def judged(self, current: Todo) -> Todo:
        self.require_quote(current.order.instructions(current))
        return current


@dataclass(frozen=True)
class AcceptedVerdict(QuotedVerdict, ToolCallVerdict):
    """The tool call carries out the order clause it quotes."""
    line = "Accepted"
    branch_rule: ClassVar[str] = (
        "Added branches need --no-owner: 12+ words with because and an absolute file:line.")

    @classmethod
    def require_branches(cls, added: int, no_owner: str | None) -> None:
        if added <= 0:
            return
        if not (no_owner and len(no_owner.split()) >= 12 and re.search(r'\bbecause\b', no_owner)
                and re.search(r'/\S+:\d+', no_owner)):
            raise TodoError(cls.branch_rule)


@dataclass(frozen=True)
class HeadVerdict(HeadReview, declared_name=None):
    """One decision on a PR head, independent of whether an order holds it."""

    def reviewed(self, goals: Goals, request: PullRequestHead, reviewer: ThreadIncarnation,
                 order: HeadReviewOrder) -> HeadReviewRecord:
        request = replace(request, head=request.current())
        subject = self.subject(goals, request)
        recorded = order.record_review(goals, request, self, reviewer)
        self.after_record(goals, recorded)
        self.post(request.repo, request.head, reviewer.name, subject)
        return recorded

    def subject(self, goals: Goals, request: PullRequestHead) -> str:
        """What the published status names; acceptance also measures what the head grows."""
        return f"#{request.pr}"

    def after_record(self, goals: Goals, record: HeadReviewRecord) -> None:
        """A rejection wakes its linked holder after the write; acceptance needs no directive."""

    def prepare_order(self, current: Todo, link: LinkedHeadReview) -> None:
        """Acceptance needs the quoted order; rejection also needs source evidence."""

    def judge_order(self, current: Todo, record: OrderHeadReviewRecord) -> Todo:
        return current

    def settles_edits(self, record: HeadReviewRecord, began: float) -> float:
        """Edits before this cut are judged by the head; only a decision judges them."""
        return began


@dataclass(frozen=True)
class DecidedHeadVerdict(HeadVerdict, declared_name=None):
    """A decision on the record says why, on GitHub and in Core."""

    reason: VerdictReason
    verb: ClassVar[str]

    def settles_edits(self, record: HeadReviewRecord, began: float) -> float:
        return record.at.latest_with(began)

    def description(self, reviewer: str, subject: str) -> str:
        return f"{self.verb} by {reviewer} on {subject}: {self.reason.text}"


@dataclass(frozen=True)
class ReviewedHeadVerdict(DecidedHeadVerdict):
    state = "success"
    verb = "Accepted"

    def subject(self, goals: Goals, request: PullRequestHead) -> str:
        project = goals.registry.snapshot().commander_project.require()
        growth = WorkBranches(Repo.selected(project.repos, request.repo.to_wire()).checkout
                              ).reviewed_growth(request.pr, request.head)
        self.require_growth(growth)
        return f"#{request.pr} (src {growth.total:+d})"

    def require_growth(self, growth: SourceGrowth) -> None:
        if growth.exceeds:
            raise TodoError(f"Source grows {growth.total:+d} code lines (limit {growth.limit}): "
                            f"{growth.listing()}. Delete the slop or accept with --growth.")


@dataclass(frozen=True)
class GrownHeadVerdict(ReviewedHeadVerdict):
    """Acceptance of a head whose source growth the reviewer explains file by file."""

    growth: GrowthReason

    def require_growth(self, growth: SourceGrowth) -> None:
        if unnamed := [path for path in growth.growing()
                       if not self.growth.names(path)]:
            raise TodoError(f"--growth names no reason for {', '.join(unnamed)} "
                            f"({growth.listing()}).")


@dataclass(frozen=True)
class RejectedHeadVerdict(DecidedHeadVerdict):
    reason: StrikeReason
    state = "failure"
    verb = "Sent back"

    def after_record(self, goals: Goals, record: HeadReviewRecord) -> None:
        record.order.rejected(goals, record)

    def prepare_order(self, current: Todo, link: LinkedHeadReview) -> None:
        link.require_evidence(current)

    def judge_order(self, current: Todo, record: OrderHeadReviewRecord) -> Todo:
        return SendBackStrike(current.revision, record.actor, record.order.previous,
                              self.reason.text, record.at,
                              evidence=record.order.evidence).apply(current)


@dataclass(frozen=True)
class UnderReviewHeadVerdict(HeadVerdict):
    state = "pending"

    def description(self, reviewer: str, subject: str) -> str:
        return f"Under review by {reviewer} on {subject}"

    def judge_order(self, current: Todo, record: OrderHeadReviewRecord) -> Todo:
        return replace(current, order=current.order.checked().under_review(record))


class HeadReviewRelation(DeclaredFamily):
    """Order relationships share one declaration family and compose their capabilities."""


class HeadOrderLink(HeadReviewRelation, declared_name=None):
    """The historical order relationship of a recorded head decision."""

    @abstractmethod
    def rejected(self, goals: Goals, record: HeadReviewRecord) -> None: ...


class HeadReviewOrder(HeadReviewRelation, declared_name=None):
    """The input that authorizes a head review's optional order change."""

    @classmethod
    def for_request(cls, todos: Sequence[Todo], request: PullRequestHead, quote: str,
                    evidence: tuple[FileLine, ...], problem: str = "") -> HeadReviewOrder:
        linked = tuple(todo for todo in todos if todo.repo == request.repo
                       and (not problem or todo.id == problem)
                       and todo.order.checked().holds_head(request))
        if not linked:
            if problem:
                raise TodoError(f"{problem} does not hold this pull request.")
            return NoHeadOrder()
        if len(linked) != 1:
            raise TodoError("More than one order holds this pull request; select --problem.")
        todo, = linked
        return OrderHeadReview(todo.revision, LinkedHeadReview.of(todo, quote, evidence))

    @abstractmethod
    def require_reviewer(self, goals: Goals, request: PullRequestHead,
                         actor: ThreadIncarnation) -> None: ...

    def director_identity(self, goals: Goals, actor: ThreadIncarnation) -> ThreadIncarnation:
        return actor

    @abstractmethod
    def record_review(self, goals: Goals, request: PullRequestHead, verdict: HeadVerdict,
                      actor: ThreadIncarnation) -> HeadReviewRecord: ...


@dataclass(frozen=True)
class NoHeadOrder(HeadReviewOrder, HeadOrderLink):
    def rejected(self, goals: Goals, record: HeadReviewRecord) -> None:
        """There is no holder to strike or wake."""

    def require_reviewer(self, goals: Goals, request: PullRequestHead,
                         actor: ThreadIncarnation) -> None:
        project = goals.registry.snapshot().commander_project.require()
        if (actor.commanding_identity != project.commander or
                request.repo not in {repo.github for repo in project.repos}):
            raise TodoError("Only this repository's commander or the director may review its heads.")

    def record_review(self, goals: Goals, request: PullRequestHead, verdict: HeadVerdict,
                      actor: ThreadIncarnation) -> HeadReviewRecord:
        record = HeadReviewRecord(request, actor, RecordedEventTime(time.time()), verdict, self)
        goals.todos.record_head(record)
        return record


@dataclass(frozen=True)
class ProblemHeadReview(OrderQuote, CitedEvidence, HeadOrderLink):
    """A problem association without an observed order ID remains an honest partial fact."""
    problem: str
    previous: Assignment

    def rejected(self, goals: Goals, record: HeadReviewRecord) -> None:
        audience = VerdictAudience.of(self.previous.owner, goals.todos.get(self.problem))
        audience.publish(goals, record.actor,
                         f"{self.problem}: {record.verdict.description(record.actor.name, f'#{record.request.pr}')}")


@dataclass(frozen=True)
class LinkedHeadReview(ProblemHeadReview):
    order_id: str

    @classmethod
    def of(cls, todo: Todo, quote: str, evidence: tuple[FileLine, ...] = ()) -> Self:
        return cls(quote=quote, problem=todo.id, order_id=todo.order.checked().id,
                   previous=todo.held(), evidence=evidence)

    def require_current(self, current: Todo, actor: ThreadIncarnation) -> None:
        current.require_commander(actor)
        current.require_assignment(self.previous)
        if current.id != self.problem or current.order.checked().id != self.order_id:
            raise TodoConflict(current)
        self.require_quote(current.order.instructions(current))


@dataclass(frozen=True)
class OrderHeadReview(HeadReviewOrder):
    expected_revision: int
    link: LinkedHeadReview

    def require_reviewer(self, goals: Goals, request: PullRequestHead,
                         actor: ThreadIncarnation) -> None:
        self.link.require_current(goals.todos.get(self.link.problem), actor)

    def director_identity(self, goals: Goals, actor: ThreadIncarnation) -> ThreadIncarnation:
        return actor.require_recorded().acting_for(goals.todos.get(self.link.problem).creator)

    def record_review(self, goals: Goals, request: PullRequestHead, verdict: HeadVerdict,
                      actor: ThreadIncarnation) -> OrderHeadReviewRecord:
        from .goal_actions import GoalPrecondition, HeadReviewedOrderGoalAction

        record = OrderHeadReviewRecord(request, actor, RecordedEventTime(time.time()), verdict, self.link)
        operation = RecordOrderHeadVerdict(self.expected_revision, record)
        goals.update_goal(self.link.previous.owner, HeadReviewedOrderGoalAction(
            expect=GoalPrecondition(goal_id=OneGoal(self.link.previous.generation)),
            problem=self.link.problem, operation=operation,
        ))
        return record


@dataclass(frozen=True)
class HeadReviewRecord:
    request: PullRequestHead
    actor: ThreadIncarnation
    at: EventTime
    verdict: HeadVerdict
    order: HeadOrderLink


@dataclass(frozen=True)
class OrderHeadReviewRecord(HeadReviewRecord):
    order: LinkedHeadReview


@dataclass(frozen=True)
class StoredHeadReview(TodoStoreRows, TypedTable):
    seq: Annotated[int, Column(primary_key=True, auto_increment=True)]
    record: HeadReviewRecord

    request: ClassVar[Generated] = Generated(
        PullRequestHead, Column(generated=f"json_extract({{record}}, '$.{FieldCodec.record_key(HeadReviewRecord, 'request')}')"),
    )
    problem: ClassVar[Generated] = Generated(
        str, Column(generated=f"json_extract({{record}}, '$.{FieldCodec.record_key(HeadReviewRecord, 'order')}.{FieldCodec.record_key(LinkedHeadReview, 'problem')}')", nullable=True),
    )
    order_id: ClassVar[Generated] = Generated(
        str, Column(generated=f"json_extract({{record}}, '$.{FieldCodec.record_key(HeadReviewRecord, 'order')}.{FieldCodec.record_key(LinkedHeadReview, 'order_id')}')", nullable=True),
    )
    indexes = (Index(("request", "seq")), Index(("problem", "order_id", "seq")))


@dataclass(frozen=True)
class RecordOrderHeadVerdict(TodoCommand):
    """The order change and its head record share the existing Todo write transaction."""
    review: OrderHeadReviewRecord

    def prepare(self, goals: Goals, current: Todo) -> Self:
        self.review.order.require_current(current, self.review.actor)
        if not current.order.checked().holds_head(self.review.request):
            raise TodoError("The reviewed pull request is not linked to this order.")
        self.review.verdict.prepare_order(current, self.review.order)
        return self

    def changed(self, current: Todo) -> Todo:
        self.review.order.require_current(current, self.review.actor)
        judged = self.review.verdict.judge_order(current, self.review)
        return replace(judged, revision=current.revision + 1)

    def write(self, db: sqlite3.Connection, current: Todo, store: TodoStore) -> Todo:
        changed = super().write(db, current, store)
        StoredHeadReview.insert_values(db, record=self.review)
        return changed


@dataclass(frozen=True)
class PeerAcceptedVerdict(AcceptedVerdict):
    peer: MessageId

    def prepare(self, goals, current):
        message = goals.bus.bus.lookup_message(self.peer)
        if (message.sender == self.actor.name or
                (message.target != current.holder and
                 current.holder not in {mention.thread for mention in message.mentions})):
            raise TodoError("--peer must name another sender's message to this unit.")
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
        replacing = [todo for todo in goals.todos.held_for_commander(
                         self.actor.commanding_identity) if todo.id == self.replacement]
        if not replacing:
            raise TodoError('The replacing problem is not held for this commander.')
        if any(todo.holder == current.holder for todo in replacing):
            raise TodoError(
                'The replacing order went to the same unit; judge its edits ok or back.')
        if len(self.reason.split()) < 6:
            raise TodoError('Say why this edit was replaced in 6+ words.')
        return self

    def judged(self, current: Todo) -> Todo:
        return current


@dataclass(frozen=True)
class EditVerdict(ToolCallVerdict, declared_name=None):
    """A verdict on a file edit: the edited path names the registered repository it was made
    in, whichever repository the order's check names."""
    path: str = field(kw_only=True)

    def edited(self, goals: Goals, current: Todo) -> tuple[Repo, Path]:
        """The repository the edit belongs to and the checkout it was made in: the holder's
        allocation of that repository for this problem, or the repository's own checkout."""
        rule = goals.registry.storage_rule
        roots = rule.roots_of(goals.registry.require(self.previous.owner))
        edited = Path(self.path).absolute()
        for repo in goals.registry.snapshot().commander_project.require().repos:
            allocated = (rule.order_checkout(roots, current.id) if repo.github == current.repo
                         else rule.repository_checkout(roots, current.id, repo.github))
            for checkout in (allocated, repo.checkout.absolute()):
                if edited.is_relative_to(checkout):
                    return repo, checkout
        raise TodoError(f"Edit {self.tool_call_id} at {self.path} is in no checkout of a "
                        f"registered repository for {current.id}.")


@dataclass(frozen=True)
class GitSettledVerdict(EditVerdict, declared_name=None):
    """Git answers at judge time that no reviewable copy of the edit is left; the commander
    supplies only the edit Core recorded."""
    added: tuple[str, ...] = field(kw_only=True)

    def prepare(self, goals: Goals, current: Todo) -> Self:
        lines = tuple(text.strip() for text in self.added if len(text.strip()) >= 4)
        if not lines:
            raise TodoError("An edit that adds no line cannot be settled by git; judge it ok or back.")
        self.refuse_held(self.edited(goals, current)[0], lines)
        return self

    @abstractmethod
    def refuse_held(self, repo: Repo, lines: tuple[str, ...]) -> None: ...

    def refuse(self, holder: str) -> None:
        raise TodoError(f"Edit {self.tool_call_id} is still on {holder}; judge it ok or back.")

    def judged(self, current: Todo) -> Todo:
        return current


@dataclass(frozen=True)
class RewrittenVerdict(GitSettledVerdict):
    """A later pushed commit holds none of the lines the edit added, so the head review of
    that commit judges what replaced it."""
    commit: str = field(kw_only=True)
    reason: str = field(kw_only=True)
    line = "Rewritten: no added line survives a later pushed commit"

    def story_text(self) -> str:
        return f"{super().story_text()} at {self.commit}: {self.reason}"

    def refuse_held(self, repo: Repo, lines: tuple[str, ...]) -> None:
        checkout = repo.checkout
        if not repo.has_commit(self.commit):
            raise TodoError(f"Commit {self.commit} is not in {repo.github.to_wire()}, where the edit "
                            "was made.")
        if not Repo.git(checkout, "branch", "-r", "--contains", self.commit).strip():
            raise TodoError(
                f"Commit {self.commit} is on no remote branch; push it before judging edits rewritten.")
        patterns = [arg for line in lines for arg in ("-e", line)]
        found = Repo.grep(checkout, "-l", "-F", *patterns, self.commit,
                          "--", f":(glob)**/{Path(self.path).name}")
        if found:
            self.refuse(found[0])


@dataclass(frozen=True)
class DroppedVerdict(GitSettledVerdict):
    """The unit discarded the edit: no branch, remote branch or worktree of its repository holds it."""
    line = "Dropped: on no branch, remote branch or worktree"

    def refuse_held(self, repo: Repo, lines: tuple[str, ...]) -> None:
        """Refuse while any place still holds every added line: the file on disk, then any
        branch, remote branch or worktree HEAD of the repository."""
        checkout = repo.checkout
        target = Path(self.path)
        if target.is_file() and all(line in target.read_text(errors="replace") for line in lines):
            self.refuse(str(target))
        refs = Repo.git(checkout, "for-each-ref", "--format=%(objectname)",
                        "refs/heads", "refs/remotes").splitlines()
        heads = [entry.removeprefix("HEAD ")
                 for entry in Repo.git(checkout, "worktree", "list", "--porcelain").splitlines()
                 if entry.startswith("HEAD ")]
        patterns = [arg for line in lines for arg in ("-e", line)]
        found = Repo.grep(checkout, "-l", "-F", "--all-match", *patterns,
                                *sorted({*refs, *heads}), "--", f":(glob)**/{target.name}")
        if found:
            self.refuse(found[0].split(":", 1)[0])


@dataclass(frozen=True)
class SendBackVerdict(ToolCallVerdict, CitedStrike, StoryEvent):
    """The tool call goes back; the same write strikes the holder and revises the order Goal."""
    reason: StrikeReason

    def send_back_times(self) -> tuple[float, ...]:
        return (self.at.recorded_at("A send-back"),)

    @property
    def line(self) -> str:
        return f"Sent back: {self.reason.text}"

    def implied_strikes(self) -> tuple[StrikeTodo, ...]:
        return (self.strike(self.expected_revision),)

    def strike(self, revision: int) -> SendBackStrike:
        return SendBackStrike(revision, self.actor, self.previous, self.reason.text,
                              self.at, evidence=self.evidence)

    def story_text(self) -> str:
        return f"{super().story_text()} (strike recorded)"

    def judged(self, current: Todo) -> Todo:
        return self.strike(current.revision).apply(current)

    def staged(self, batch: TodoBatch, call: JudgedCall) -> None:
        """A send-back revises the holder's Goal and posts, so it records after the transaction."""
        batch.alone.append(call)

    def record(self, goals: Goals, problem: str) -> Todo:
        changed = goals.send_back(problem, self)
        self.audience.of(self.previous.owner, changed).publish(
            goals, self.actor, f"{changed.id}: {self.story_text()}",
        )
        return changed


@dataclass(frozen=True)
class EditSendBackVerdict(SendBackVerdict, EditVerdict):
    """A send-back of a file edit cites lines in the checkout the edit was made in, as it
    stands on disk."""

    def prepare(self, goals: Goals, current: Todo) -> Self:
        self.require_cited(self.edited(goals, current)[1], WorkingTree())
        return self


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

    def changed(self, current: Todo) -> Todo:
        current.require_commander(self.actor)
        if self.strike not in current.live_strikes(self.strike.previous.owner):
            raise TodoConflict(current)
        _text(self.reason, "Void reason", 2000)
        StrikeReason(self.reason)
        voided = replace(current, voided=(*current.voided, self), revision=current.revision + 1)
        return voided.holder_state.after_void(voided)


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

    def changed(self, current: Todo) -> Todo:
        self.actor.require_may_change(current)
        current.require_assignment(self.previous)
        return replace(current, assignment=None, released=(*current.released, self),
                       revision=current.revision + 1)

    def retained_assignments(self, current: Todo, pulls: PullRequests) -> tuple[Assignment, ...]:
        return pulls.retained_assignments(current, self.previous)

    def restored(self, current: Todo) -> Todo:
        """Unstrike and the watcher restore this recorded assignment through the same write."""
        if current.struck_out(self.previous.owner):
            return current
        return replace(current, assignment=self.previous)


@dataclass(frozen=True)
class LimitReleaseTodo(ReleaseTodo):
    """The strike limit declares its release's meaning when it records it."""

    def after_void(self, voided: Todo) -> Todo:
        return self.restored(voided)


@dataclass(frozen=True)
class RestoreHolderTodo(TodoCommand):
    """Restore released work without reissuing its order or replacing the unit's goal."""
    actor: ThreadIncarnation
    previous: Assignment

    def validate(self, db: sqlite3.Connection, current: Todo, store: TodoStore) -> None:
        snapshot = RegistryStore.at(store.root).read_snapshot_unlocked()
        if not self.previous.running_holders(snapshot):
            raise TodoConflict(current)
        self.previous.require_available_in(db, current.id, self.actor, snapshot)

    def changed(self, current: Todo) -> Todo:
        self.actor.require_may_change(current)
        current.state.require_open(current)
        if current.holder_state.previous_assignment() != self.previous:
            raise TodoConflict(current)
        restored = current.holder_state.restored(current)
        return (replace(restored, revision=current.revision + 1)
                if restored != current else current)


@dataclass(frozen=True)
class CorrectOrderTodo(TodoOperation, declared_name=None):
    """The commander replaces a wrong part of an open order.

    The order keeps its id, holder, review timer and strikes; a correction is not a resend."""
    actor: ThreadIncarnation
    reason: str
    previous: object
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()), kw_only=True)

    def prepare(self, goals: Goals, current: Todo) -> Self:
        OrderCorrectionReason(self.reason)
        return self

    def changed(self, current: Todo) -> Todo:
        current.require_commander(self.actor)
        current.state.require_open(current)
        _text(self.reason, "Correction reason", 2000)
        return replace(current, order=current.order.corrected(self), revision=current.revision + 1)

    def corrected[Order: OpenProblemOrder](self, order: Order) -> Order:
        if self.previous != self.part(order):
            raise TodoError("The corrected part changed since it was read; read it again.")
        return replace(self.replaced(order), corrections=(*order.corrections, self))

    @abstractmethod
    def part(self, order: OpenProblemOrder) -> object:
        """The order's current value of the part this correction replaces."""

    @abstractmethod
    def replaced[Order: OpenProblemOrder](self, order: Order) -> Order:
        """The order with this correction's part replaced."""


@dataclass(frozen=True)
class CorrectCheckTodo(CorrectOrderTodo):
    """The commander replaces a wrong check of an open order."""
    previous: OrderCheck
    check: OrderCheck

    def prepare(self, goals: Goals, current: Todo) -> Self:
        self.check.require_directory()
        current.order.checked().require_check_acceptance(
            self.check, goals.registry.storage_rule.checkout_base
        )
        return super().prepare(goals, current)

    def part(self, order: OpenProblemOrder) -> OrderCheck:
        return order.check

    def replaced[Order: OpenProblemOrder](self, order: Order) -> Order:
        self.check.require_writable()
        return replace(order, check=self.check)


@dataclass(frozen=True)
class CorrectRunTodo(CorrectOrderTodo):
    """The commander replaces the run that closes an open order, e.g. one naming deleted code."""
    previous: OrderRun
    run: OrderRun

    def part(self, order: OpenProblemOrder) -> OrderRun:
        return order.run

    def replaced[Order: OpenProblemOrder](self, order: Order) -> Order:
        return replace(order, run=self.run)


@dataclass(frozen=True)
class WordingCorrection(TodoOperation):
    """Core's answer to a published unit message whose words hide what happened; no strike."""
    message_id: MessageId
    words: tuple[str, ...]

    def authored_by(self, actor: ThreadIncarnation) -> Self:
        return self

    def changed(self, current: Todo) -> Todo:
        corrections = (*current.corrections, self)
        return replace(current, corrections=corrections, revision=current.revision + 1)


@dataclass(frozen=True)
class OrderMessage(TodoOperation):
    """The bus message the commander sent this order in; a verdict citing it lands here."""
    actor: ThreadIncarnation
    message_id: MessageId

    def changed(self, current: Todo) -> Todo:
        current.require_commander(self.actor)
        return replace(current, messages=(*current.messages, self), revision=current.revision + 1)


@dataclass(frozen=True)
class RecordPullRequest(TodoCommand):
    actor: ThreadIncarnation
    link: PullRequestLink
    generation: GoalId

    def changed(self, current: Todo) -> Todo:
        current.require_actor(self.actor, self.generation)
        if self.link.repo != current.repo:
            raise TodoError("A recorded pull request must belong to the todo's repository.")
        order = current.order.checked()
        if self.link in order.pull_requests:
            return current
        return replace(current, revision=current.revision + 1, order=order.linked_to(self.link))


@dataclass(frozen=True)
class ServeTodo(TodoOperation):
    """The commander names the plan phase an open order serves; it keeps its id and holder."""
    actor: ThreadIncarnation
    serves: type[OrderPhase]

    def changed(self, current: Todo) -> Todo:
        current.require_commander(self.actor)
        return replace(current, order=current.order.for_phase(self.serves),
                       revision=current.revision + 1)


@dataclass(frozen=True)
class ClaimOrder(TodoCommand):
    """Only the holder records a claim; its answer is published from the retained value."""

    actor: ThreadIncarnation
    generation: GoalId
    claim: OrderClaim

    def record(self, goals: Goals, problem: str) -> Todo:
        """Retain the publication's actual address before saving its content identity."""
        with NativeBoundary(goals.bus) as boundary:
            current = goals.todos.get(problem)
            target = IncarnationThreadTarget(
                goals.todos.owner_request(current.request.require()).commander)
            target.require_publication(
                self.claim.message, boundary.registry, goals.bus.require_channel)
            message = replace(
                self.claim.message,
                sender=boundary.registry.canonical_name(self.claim.message.sender),
                target=target.selected_name(boundary.registry),
            )
            prepared = replace(self, claim=replace(self.claim, message=message))
            return super(ClaimOrder, prepared).record(goals, problem)

    def changed(self, current: Todo) -> Todo:
        current.state.require_open(current)
        current.require_assignee(self.actor, self.generation)
        order = current.order.checked().claimed(self.claim)
        return replace(current, order=order, revision=current.revision + 1)

    def after_record(self, goals: Goals, changed: Todo) -> None:
        goals.bus.publisher.publish_ordinary(
            self.claim.message, destination=IncarnationThreadTarget(
                goals.todos.owner_request(changed.request.require()).commander),
            origin=AgentOrigin())


@dataclass(frozen=True)
class DeliverTodo(TodoOperation, declared_name=None):
    """Internal delivery command; the order-done tool owns its wire boundary and claims."""

    actor: ThreadIncarnation
    delivery: OrderDelivery
    order_id: str
    verified_claims: Annotated[dict[ProgressClaim, tuple[str, ...]], WireTransient()] = field(
        default_factory=dict, kw_only=True, repr=False, compare=False,
    )

    def require_current(self, current: Todo) -> None:
        super().require_current(current)
        current.require_commander(self.actor)
        current.state.require_open(current)
        current.order.require_delivery(self.order_id)

    def prepare(self, goals, current):
        self.require_current(current)
        UnitStorage.read(
            goals.registry.storage_rule, goals.registry.require(current.held().owner),
        ).require_retired(current.id)
        verified = self.delivery.verified(current, goals.registry, self.verified_claims)
        try:
            delivered = asyncio.run(verified)
        except OrderCheckFailedError as error:
            self.delivery.check_failed(goals, current, error)
            raise
        return replace(self, delivery=delivered)

    def changed(self, current):
        self.delivery.at.require_delivery()
        return current.order.checked().scope.completed(current, self.delivery)


@dataclass(frozen=True)
class CloseOrderTodo(TodoOperation, declared_name=None):
    """The commander records the order's terminal state and closes its problem in one revision."""
    actor: ThreadIncarnation

    @abstractmethod
    def closed(self, current: Todo) -> ProblemOrder: ...

    def require_current(self, current: Todo) -> None:
        super().require_current(current)
        current.require_commander(self.actor)

    def changed(self, current):
        return DoneTodoState.closed(current, self.closed(current))


@dataclass(frozen=True)
class InstallTodo(CloseOrderTodo):
    works: str
    artifact: str

    def prepare(self, goals: Goals, current: Todo) -> InstallTodo:
        self.require_current(current)
        current.order.require_installation()
        return self

    def closed(self, current: Todo) -> ProblemOrder:
        if not Path(self.artifact).is_file():
            raise TodoError("Retain the installed application artifact before closing.")
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
    generation: GoalId | None

    def prepare(self, goals, current):
        self.state.prepare_change(goals, current)
        return self

    def changed(self, current):
        return current.state.change(current, self)


@dataclass(frozen=True, slots=True)
class OwnerRequest(TodoStoreRows, TypedTable, SharedFileTables, DurableTables):
    """One owner's request, independent of any current turn goal."""

    indexes = (Index(("commander_created_at", "state", "created_at")),)

    id: Annotated[OwnerRequestId, Column(primary_key=True, storage=TextStorage)]
    commander: RecordedThreadIncarnation
    commander_created_at: ClassVar[Generated] = Generated(
        float, Column(generated="json_extract({commander}, '$.created_at')"),
    )
    text: str
    state: RequestState = OpenRequestState()
    directives: tuple[RequestDirective, ...] = ()
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise TodoError("An owner request needs its text.")

    @classmethod
    def new(cls, commander: ThreadIncarnation, text: str) -> Self:
        return cls(OwnerRequestId.new(), commander.require_recorded(), text)

    def require_commander(self, commander: ThreadIncarnation) -> None:
        if self.commander != commander.require_recorded():
            raise TodoError(f"Request {self.id.value} belongs to another commander.")

    def require_open(self, commander: ThreadIncarnation) -> OpenRequestState:
        self.require_commander(commander)
        return self.state.require_open(self.id)

    def at_revision(self, directives: tuple[RequestDirective, ...]) -> RequestAtRevision:
        if self.directives[:len(directives)] != directives:
            raise TodoError(f"Request {self.id.value} history is not a recorded directive prefix.")
        return RequestAtRevision(self.id, len(directives))

    def directive_messages(
        self, source: BusReader, responders: frozenset[str],
    ) -> tuple[tuple[RequestDirective, Message], ...]:
        """Select exact unanswered references before loading their originals."""
        unanswered = {reference.seq for reference in source.unanswered_references(
            tuple(directive.seq for directive in self.directives), responders)}
        return tuple((directive, directive.original(source)) for directive in self.directives
                     if directive.seq in unanswered)


@dataclass(frozen=True, slots=True)
class Todo(TodoStoreRows, TypedTable):
    unit_index: ClassVar[ExpressionIndex] = ExpressionIndex((
        Assignment.sql_field('owner'), Assignment.sql_field('owner_created'), '{state}',
    ))
    problem_index: ClassVar[ExpressionIndex] = ExpressionIndex((ProblemNumber.sql_number('{id}'),))
    serving_request: ClassVar[str] = (
        f'json_extract("order",\'$.{ProblemOrder.family_discriminator}\') '
        f'IN {DeliverableProblemOrder.sql_choices()}'
    )
    indexes = (Index(("creator", "creator_created", "state")), Index(("state",)),
               unit_index, problem_index, Index(("request",)),
               Index(("request",), where=serving_request))

    id: Annotated[str, Column(primary_key=True)]
    repo: Annotated[RepositoryName, Column(storage=TextStorage)]
    text: str
    creator: str
    creator_created: float
    state: type[TodoState]
    revision: Annotated[int, Column(check="revision>0")]
    request: Annotated[
        RequestLink, Column(storage=TextStorage, nullable=True, references=TableReference(OwnerRequest, ("id",))),
    ]
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

    @classmethod
    def rows(cls, db: sqlite3.Connection, where: str, parameters: tuple) -> tuple[Todo, ...]:
        return tuple(cls.read(db.execute(
            f"SELECT * FROM {cls.declared_name} WHERE {cls._sql(where)} ORDER BY rowid",
            parameters)))

    @property
    def repository(self) -> Repo:
        return Repo(checkout=self.order.checked().check.directory, github=self.repo)

    @property
    def judged_calls(self) -> tuple[str, ...]:
        return tuple(call for verdict in self.verdicts for call in verdict.judged_calls())

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

    @classmethod
    def ordered(cls, todo_id: str, repo: RepositoryName, text: str, creator: ThreadIncarnation,
                assignment: Assignment, review_at: EventTime,
                order: PendingProblemOrder | MergedProblemOrder,
                request: OwnerRequestId) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 2000)
        review_at.require_recorded("A review time")
        return cls(todo_id, repo, text, creator.name, creator.wire_birth, OpenTodoState,
                   1, request, assignment, order, review_at)

    @property
    def pending(self):
        return bool(self.order.items_with(Pending, self))

    def story_sources(self) -> tuple[StorySource, ...]:
        implied = frozenset(strike for verdict in self.verdicts
                            for strike in verdict.implied_strikes())
        return (
            self.order, *(strike for strike in self.strikes if strike not in implied),
            *self.voided, *self.released, *self.verdicts,
        )

    def story(self) -> ProblemStory:
        events = tuple(sorted(
            (event for source in self.story_sources() for event in source.story()),
            key=lambda event: event.at.chronology(),
        ))
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
    def require_assignee(self, actor: ThreadIncarnation, generation: GoalId | None) -> None:
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

    def require_released_order(self, owner: ThreadIncarnation, goal_id: GoalId) -> None:
        if self.holder_state.owned_by(owner, goal_id):
            raise TodoConflict(self)

    @property
    def latest_message(self) -> OrderMessage:
        if not self.messages:
            raise TodoError(f"{self.holder} holds {self.id}, but no order message was recorded.")
        return self.messages[-1]

    def require_fold(self, target: Todo, why: str) -> None:
        self.state.require_open(self)
        target.state.require_open(target)
        if self.id == target.id:
            raise TodoError("Fold needs two different open problems.")
        if len(why.split()) < 8:
            raise TodoError("Say why they are the same problem in 8+ words.")
        self.order.folded(target.id, why)

    @property
    def acting_assigner(self) -> ThreadIncarnation:
        """Who assigned this order last, acting under its commander: a reissue acts as them,
        so a sibling fork's competing order on the unit is still refused."""
        return self.holder_state.previous_assignment().parent_identity.acting_for(
            self.creator_identity)

    def require_commander(self, actor: ThreadIncarnation) -> None:
        if self.creator_identity != actor.commanding_identity:
            raise NotOrderCommander(self, actor)

    def require_actor(self, actor: ThreadIncarnation, generation: GoalId | None) -> None:
        if self.creator_identity != actor.commanding_identity:
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
                request=proposed.request,
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
    thread.work_state.require_assignment()
    return _participant(thread)


def _participant(thread: Thread) -> ThreadIncarnation:
    _text(thread.name, "Thread name", 128)
    try:
        thread.incarnation.require_recorded()
    except ValueError as error:
        raise TodoError("Creator requires a recorded incarnation") from error
    return thread.incarnation


@dataclass(frozen=True)
class ProblemAllocation(TodoStoreRows, TypedTable):
    """Only issued, unsent numbers; creating a Todo removes its allocation row.

    SQLite's sequence remembers issuance, while Todo.id owns every created identity.
    """

    number: Annotated[int, Column(primary_key=True, auto_increment=True)]
    renamed_from = ("problem_number",)

    @classmethod
    def reserve(cls, db: sqlite3.Connection, text: str) -> ProblemNumber:
        try:
            problem = ProblemNumber.parse(text)
        except ValueError as error:
            raise TodoError(f'Malformed problem ID: {text!r}') from error
        return cls.issue(db, problem)

    @classmethod
    def issue(cls, db: sqlite3.Connection, problem: ProblemNumber) -> ProblemNumber:
        cls(problem.number).insert(db)
        return problem

    @classmethod
    def allocate(cls, db: sqlite3.Connection) -> ProblemNumber:
        number, = db.execute(
            f'SELECT max(COALESCE((SELECT max({Todo.problem_index.term_sql(Todo)}) '
            f'FROM "{Todo.declared_name}"), 0), '
            'COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?), 0)) + 1',
            (cls.declared_name,)).fetchone()
        return cls.issue(db, ProblemNumber(number))


class OwnerRequestStore(DurableStore):
    """Request lifetime and serving-work queries share the store's transaction."""

    def add_request(self, commander: ThreadIncarnation, text: str) -> OwnerRequest:
        request = OwnerRequest.new(commander, text)
        with self.writing() as db:
            request.insert(db)
        return request

    def owner_request(self, request: OwnerRequestId) -> OwnerRequest:
        with self.snapshot() as db:
            return OwnerRequest.require(db, id=request)

    def open_requests(
        self, commander: ThreadIncarnation, *, limit: int = -1, descending: bool = True,
    ) -> tuple[OwnerRequest, ...]:
        with self.snapshot() as db:
            return tuple(OwnerRequest.select(
                db, where="commander_created_at=?",
                parameters=(commander.require_recorded().created_at,), state=OpenRequestState(),
                order_by=("created_at", "id"), descending=descending, limit=limit,
            ))

    def record_directive(self, request: OwnerRequestId, directive: RequestDirective) -> None:
        with self.writing() as db:
            current = OwnerRequest.require(db, id=request)
            current.state.require_open(current.id)
            replace(current, directives=(*current.directives, directive)).upsert(db)

    def complete_request(
        self, commander: ThreadIncarnation, request: OwnerRequestId, reason: str,
    ) -> OwnerRequest:
        with self.writing() as db:
            current = OwnerRequest.require(db, id=request)
            state = current.require_open(commander)
            completed = replace(current, state=state.complete(
                request, self._serving(db, request), reason,
            ))
            completed.upsert(db)
        return completed

    @staticmethod
    def _serving(db: sqlite3.Connection, request: OwnerRequestId) -> tuple[Todo, ...]:
        return tuple(Todo.select(db, request=request, where=Todo.serving_request))

    def serving_orders(self, request: OwnerRequestId) -> tuple[Todo, ...]:
        """Pending work and unaccepted claims, through the request's partial index."""
        with self.snapshot() as db:
            return self._serving(db, request)

    def for_request(self, request: OwnerRequestId) -> tuple[Todo, ...]:
        """Every order that served this independent request, open or done."""
        with self.snapshot() as db:
            return tuple(Todo.select(db, request=request))


@dataclass(frozen=True, slots=True)
class TodoStore(OwnerRequestStore):
    """One SQLite row owns both todo state and its exclusive assignment.

    Assignment identity and revision deduplicate known operations. An uncertain
    input is not permission to replay it; inspect its original retained result.
    This store never launches a child.
    """

    root: Path
    filename: ClassVar[str] = "todos.sqlite3"
    tables = TodoStoreRows

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        try:
            with (RegistryStore.at(self.root).locked(mode=SharedLock()),
                  super(TodoStore, self).writing() as connection):
                yield connection
        except sqlite3.Error as error:
            raise TodoStoreUnwritable(
                f"{self.path}: {type(error).__name__}: {error}"
            ) from error

    def allocate_problem(self) -> str:
        with self.writing() as db:
            return str(ProblemAllocation.allocate(db))

    @classmethod
    def require_unit_in(cls, db: sqlite3.Connection, unit: ThreadIncarnation,
                        assigner: ThreadIncarnation) -> None:
        """The original open Todo owns who assigns its unit, under this transaction.

        Its commander outranks the fork that assigned it, so an order a fork left open,
        even one whose fork has ended, is the commander's to replace; sibling forks
        stay refused, since a fork never created a problem itself. An order whose work waits
        only on install holds no unit, as ProblemOrder.occupies_holder answers."""
        for todo in cls.held_in(db,
                f'NOT (({Assignment.sql_field("parent")}=? '
                f'AND {Assignment.sql_field("parent_created")}=?) '
                'OR (creator=? AND creator_created=?))',
                (assigner.name, assigner.wire_birth) * 2, unit=unit):
            if not todo.order.occupies_holder():
                continue
            raise TodoError(
                f"{unit.name} is assigned by {todo.held().parent} on open order "
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
        authority = commander.commanding_identity
        found = self._select('id=? AND creator=? AND creator_created=?',
                             (problem, authority.name, authority.wire_birth), reader=self._open)
        if len(found) != 1 or found[0].holder != unit:
            raise TodoError(f'{unit} holds no order on {problem}')
        return found[0]

    def drop(self, commander: ThreadIncarnation, unit: str, problem: str, by: str) -> Todo:
        old = self.resolve_order(commander, unit, problem)
        newer = self.resolve_order(commander, unit, by)
        if not old.pending or not newer.pending or newer.order.at.at <= old.order.at.at:
            raise TodoError('Only a live newer order to the same unit replaces an old one.')
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
                raise TodoError(f'{problem} is already held by {target.holder}')
            linked = target.order.linked(source.order)
            replace(target, order=linked,
                    prior_orders=target.orders_before(linked, source.prior_orders),
                    assignment=source.held(), request=source.request, review_at=source.review_at,
                    messages=source.messages, revision=target.revision + 1).upsert(db)

    @staticmethod
    def _open(db: sqlite3.Connection, where: str, parameters: tuple,
              state: type[TodoState] = TodoState) -> tuple[Todo, ...]:
        states = state.open_names()
        selected = ','.join('?' for state in states)
        return Todo.rows(db, f"state IN ({selected}) AND ({where})", (*states, *parameters))

    @classmethod
    def held_in(cls, db: sqlite3.Connection, where: str, parameters: tuple,
                *, unit: ThreadIncarnation) -> tuple[Todo, ...]:
        """The one open-unit query, used by every reader and writer."""
        terms = Todo.unit_index.terms[:-1]
        match = f"({', '.join(terms)})=({','.join('?' for term in terms)})"
        return cls._open(db, f'{match} AND ({where})',
                         (unit.name, unit.wire_birth, *parameters))

    def _select(self, where: str, parameters: tuple, *,
                reader: Callable[[sqlite3.Connection, str, tuple], tuple[Todo, ...]] = Todo.rows,
                ) -> tuple[Todo, ...]:
        """An absent store has no rows; only this boundary opens a read snapshot."""
        if not self.path.exists():
            return ()
        with self.snapshot() as db:
            return reader(db, where, parameters)

    @classmethod
    def _one(cls, rows: tuple[Todo, ...], todo_id: str) -> Todo:
        if not rows:
            raise TodoError(f"Unknown todo: {todo_id}.")
        return rows[0]

    @classmethod
    def _current(cls, db: sqlite3.Connection, todo_id: str) -> Todo:
        return cls._one(Todo.rows(db, "id=?", (todo_id,)), todo_id)

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

    def for_goal(self, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[Todo, ...]:
        return self._select(Assignment.sql_field('generation') + '=?', (goal_id,),
                            reader=partial(self.held_in, unit=owner))

    def held_orders(self) -> tuple[Todo, ...]:
        return self._select("assignment IS NOT NULL", (),
                            reader=partial(self._open, state=OpenTodoState))

    def reassignable(self) -> tuple[Todo, ...]:
        """Work Core released after its objective strikes; never a commander's released backlog.
        No clock releases work: a held order stays held until its commander moves it."""
        return tuple(todo for todo in self._select("assignment IS NULL", (),
                     reader=partial(self._open, state=OpenTodoState))
                     if todo.pending and todo.handed_out_on_release)

    def record_merges(self, completed: tuple[Todo, ...]) -> None:
        """Record only merge changes whose observed revision still holds."""
        if not completed:
            return
        with self.writing() as db:
            for result in completed:
                current = self._current(db, result.id)
                if current.revision + 1 == result.revision:
                    result.upsert(db)

    def holds_pending(self, owner: ThreadIncarnation) -> bool:
        """Whether a problem assigned to owner pends, under any generation; its Goal stays open."""
        return any(todo.pending for todo in self._select(
            '1', (), reader=partial(self.held_in, unit=owner)))

    def for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._select("creator=? AND creator_created=?",
                            (commander.name, commander.wire_birth), reader=self._open)

    @staticmethod
    def timers_up_in(held: Sequence[Todo], now: float,
                     progress: Mapping[str, OrderProgress]) -> tuple[tuple[Todo, RecordedEventTime], ...]:
        """Review rounds from this read's orders and PR facts, excluding installation waits."""
        return tuple((todo, up) for todo in held for up in todo.timer_up(now)
                     if not todo.order.waiting_on_install(todo, progress))

    def held_for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._select("creator=? AND creator_created=? AND assignment IS NOT NULL",
            (commander.name, commander.wire_birth), reader=self._open)

    def held_by(self, commander: ThreadIncarnation, unit: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._select('creator=? AND creator_created=?',
            (commander.name, commander.wire_birth), reader=partial(self.held_in, unit=unit))

    def require_recurrence_declaration(
        self, order: CheckedProblemOrder, where: FileLine, root: Path,
        slop: tuple[Slop, ...], now: float,
    ) -> None:
        previous = tuple(line for todo in self.list(Repo.of(root).github)
                         for line in todo.recurrence((order.owner, where), root, now))
        if previous and not any(
            item.derive.declaration_elsewhere(item.by.line, where, root) for item in slop
        ):
            raise TodoError('\n'.join((*previous,
                'the same file broke again, name the fact written in more than one place '
                'and the declaration that replaces the copies.')))

    def history_for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        return self._select("creator=? AND creator_created=?", (commander.name, commander.wire_birth))

    def units(self, commander: ThreadIncarnation) -> frozenset[ThreadIncarnation]:
        """The threads this commander gives orders to: everyone who holds or held one of its
        problems, by birth, so a renamed holder stays one unit."""
        return frozenset(
            assignment.owner_identity for todo in self.history_for_commander(commander)
            for assignment in (*todo.holder_state.current_assignments(),
                               *(release.previous for release in todo.released)))

    def create(
        self,
        todo_id: str,
        repo: RepositoryName,
        text: str,
        *,
        creator: Thread,
        owner: Thread,
        assigner: ThreadIncarnation,
        generation: GoalId,
        review_at: EventTime,
        order: PendingProblemOrder,
        request: OwnerRequestId,
    ) -> Todo:
        number = ProblemNumber.parse(todo_id)
        identity, assignee = _participant(creator), _thread(owner)
        DirectiveText.require(text)
        order.require_wording()
        order.require_writable()
        proposed = Todo.ordered(
            todo_id, repo, text, identity, Assignment.of(assignee, assigner, generation),
            review_at, order, request)
        with self.writing() as db:
            proposed.held().require_available_in(
                db, todo_id, assigner,
                RegistryStore.at(self.root).read_snapshot_unlocked())
            OwnerRequest.require(db, id=request).require_open(identity)
            proposed.insert_if_absent(db)
            current = self._current(db, todo_id)
            current.require_same_creation(proposed)
            current.require_assignment(proposed.assignment)
            ProblemAllocation.delete(db, number=number.number)
            return current

    def record_head(self, record: HeadReviewRecord) -> None:
        with self.writing() as db:
            StoredHeadReview.insert_values(db, record=record)

    def head_reviews(self, request: PullRequestHead) -> tuple[HeadReviewRecord, ...]:
        """An indexed exact-head lookup, newest committed verdict first."""
        with self.snapshot() as db:
            return tuple(row.record for row in StoredHeadReview.select(
                db, request=request, order_by=("seq",), descending=True, limit=1,
            ))

    def unjudged_since(self, todo: Todo) -> float:
        """The problem's work starts at its first order; a send-back reissues the order but
        judges none of the edits before it, so only a decided head review moves the cut."""
        with self.snapshot() as db:
            reviews = StoredHeadReview.select(db, problem=todo.id)
        began = min(order.at.recorded_at("An order") for order in todo.order_history)
        return max((began, *(row.record.verdict.settles_edits(row.record, began)
                             for row in reviews)))

    def apply(self, todo_id: str, command: TodoCommand) -> Todo:
        with self.writing() as db:
            current = self._current(db, todo_id)
            command.write(db, current, self)
            return self._current(db, todo_id)

    def correct(
        self, unit: ThreadIncarnation, message_id: MessageId, text: str,
    ) -> tuple[Todo, ...]:
        """Correct every open order the unit holds when its published text uses jargon."""
        if not self.path.exists() or not (words := tuple(PlainText.found(text))):
            return ()
        with self.writing() as db:
            held = self.held_in(db, '1', (), unit=unit)
            corrected = tuple(WordingCorrection(todo.revision, message_id, words).apply(todo)
                              for todo in held)
            for todo in corrected:
                todo.upsert(db)
            return corrected

    def fire(
        self, unit: ThreadIncarnation, actor: ThreadIncarnation, reason: StrikeReason,
    ) -> tuple[Todo, ...]:
        """Release every open problem the unit holds from this commander in one write; each
        records the same reason, and its order and strikes stay."""
        with self.writing() as db:
            commander = actor.commanding_identity
            held = tuple((todo, todo.held()) for todo in self.held_in(
                db, 'creator=? AND creator_created=?',
                (commander.name, commander.wire_birth), unit=unit))
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
        generation: GoalId,
        request: OwnerRequestId,
        review_at: EventTime,
        order: PendingProblemOrder,
        traced: FileLine | NoSourceReference = NoSourceReference(),
    ) -> Todo:
        return self.apply(todo_id, AssignTodo.by(assigner, expected_revision, _thread(owner),
                                                generation, review_at, order, request, traced))
