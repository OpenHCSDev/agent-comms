from __future__ import annotations

import argparse
import math
import time
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from functools import cache, cached_property
from itertools import combinations
from pathlib import Path
from subprocess import TimeoutExpired
from typing import ClassVar, Self, cast

from agent_comms.cli_verbs.declared_verb import DeclaredVerb
from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import (
    FailingPass,
    ServicePasses,
    ServicePassStore,
)
from agent_comms.errors import RejectedEnforcementError, RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity, ResidentMemory
from agent_comms.execution.root_runtime import RootRuntimeStore
from agent_comms.execution.turn_bound import LastTurns, RecentTurnsSince
from agent_comms.execution.turn_digests import OrderEditCountRead, TurnDigestRead
from agent_comms.heartbeat_subjects import (
    AnswerSubject,
    HeadSubject,
    HeartbeatSubject,
    TeammateSubject,
)
from agent_comms.messaging.bus import BusReader
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.nominal.declared_family import DeclaredFamily, RegisteredFamily
from agent_comms.nominal.field_codec import (
    DefaultedName,
    DefaultExternalName,
    FieldCodec,
    OrderId,
)
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.planning.goal_management import Goals
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.orders import (
    ClosedHeadReading,
    EditWorkplaces,
    HeadReviewRecord,
    HeadVerdict,
    OrderClaim,
    OrderProgress,
    OwnerRequest,
    PresentCheckout,
    ProgressRead,
    ProgressSource,
    PullRequest,
    StoredClosedHead,
    StoredHeadReview,
    Todo,
    TodoConflict,
    TodoStore,
)
from agent_comms.planning.owner_requests import OwnerRequestId, RequestDirective
from agent_comms.planning.plan_phase_base import PlanPhase
from agent_comms.planning.plan_reading import PlanReading, PlanReadings
from agent_comms.planning.pull_requests import MergeBlock
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.head_review import HeadReviewOrder, PullRequestHead
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.storage_rules import MemoryUse, StorageRule, UnitStorage
from agent_comms.projects.work_branches import (
    BranchDifference,
    SourceGrowth,
    UnparsedSource,
    WorkBranchError,
)
from agent_comms.registry.registry_document import (
    RegisteredTeamProject,
    RegistrySnapshot,
)
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.recorded_time import EventTime, RecordedEventTime
from agent_comms.task_attachments.owner_notices import OrderNotice
from agent_comms.team.team_service import TeamService
from agent_comms.transcript.transcript_events import TurnDigest


class TeammateEvidence(ABC):
    """The original holder facts consumed by standing-rule scans."""

    thread: Thread
    todos: tuple[Todo, ...]
    frame: HeartbeatFrame

    @abstractmethod
    def merges(self) -> dict[str, OrderProgress]: ...


class ObservedTeammate(ABC):
    """A classified holder supplies its original evidence and readable identities."""

    unit: ThreadIncarnation
    source: TeammateEvidence

    @abstractmethod
    def readable_units(self) -> tuple[ThreadIncarnation, ...]: ...


@dataclass(frozen=True)
class Sign:
    """One occurrence of a degraded signal. Its key is what makes it new: a load round. The
    same occurrence never stops the teammate twice; its line is how it reads now."""

    signal: type[Degradation]
    key: tuple[object, ...]
    line: str = field(compare=False)

    @property
    def mark(self) -> str:
        return self.signal.mark(self.key)


class Degradation(DeclaredFamily, affix="Degradation"):
    """One signal that the fleet is getting away from its teammate."""

    @classmethod
    @abstractmethod
    def found(cls, comms: Comms, frame: HeartbeatFrame) -> tuple[Sign, ...]:
        """How the signal shows now, one sign per occurrence; empty while it does not."""

    @classmethod
    def mark(cls, key: tuple[object, ...]) -> str:
        """One occurrence as text, the same however often it is read."""
        return " ".join((cls.declared_name, *map(str, key)))


class BlocksRelease:
    """A degraded signal the teammate clears this turn, so a chokehold holds its units until
    the signal is gone: a quote is the answer, never the relief."""


class StaleTodoDecision(RejectedEnforcementError):
    """A rule cannot refresh its decision after the row changes."""

    reason: ClassVar[str] = "todo decision is stale"

    def __init__(self, current: Todo) -> None:
        self.current = current
        super().__init__(self.explanation())

    def explanation(self) -> str:
        return f"{self.current.id}: decision is stale at revision {self.current.revision}"


class RepeatedTodoChange(StaleTodoDecision):
    """The one refreshed decision also lost its observed revision."""

    reason: ClassVar[str] = "todo changed again"

    def __init__(self, previous: Todo, current: Todo) -> None:
        self.previous = previous
        super().__init__(current)

    def explanation(self) -> str:
        return (f"{self.previous.id}: revision {self.previous.revision} changed again "
                f"to revision {self.current.revision}")


class ClearingVerb(DeclaredVerb, declared_name=None):
    """A declared action owns the standing rules it clears and the hints they render."""

    clears: ClassVar[tuple[type[StandingRule], ...]] = ()

    def __init_subclass__(cls, **options: object) -> None:
        """A verb names the owed answers it clears; each rule's hint is rendered from them."""
        super().__init_subclass__()
        for rule in vars(cls).get("clears", ()):
            rule.cleared_by(cls)

    @classmethod
    def source_review_hint(cls) -> bool:
        """A generic source-review line may offer this action without carrier evidence."""
        return True

    @classmethod
    def review_allowed(cls, verdicts: tuple[type[HeadVerdict], ...]) -> bool:
        """Ordinary verbs do not select a head verdict."""
        return True

    def resolves_claim(self, unit: str, problem: str) -> bool:
        """Ordinary messages and actions do not judge a delivery claim."""
        return False

    @classmethod
    def commands(cls, values: Mapping[str, str]) -> tuple[str, ...]:
        """The commands its parser exposes, filled from this line's observed arguments.

        A parameter variant with no parser contributes no second command. Required review
        prose remains the reviewer's, not something the board can manufacture.
        """
        verbs = argparse.ArgumentParser(prog=cls.tool()).add_subparsers()
        cls.add_parser(verbs)
        commands = []
        for parser in verbs.choices.values():
            placeholder = parser._get_formatter()._format_args
            words = [cls.tool(), cls.declared_name]
            for action in parser._actions:
                if action.dest in values:
                    words += [*action.option_strings[:1], values[action.dest]]
                elif action.required and not action.option_strings:
                    words.append(placeholder(action, action.dest))
            commands.append(' '.join(words))
        return tuple(commands)


class StandingRule(DefaultedName, RegisteredFamily, affix="StandingRule", discovery_package="agent_comms.team"):
    @classmethod
    @abstractmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @abstractmethod
    def broke_after(self, after: float) -> bool: ...

    clearing_verbs: ClassVar[tuple[type[ClearingVerb], ...]] = ()

    @classmethod
    def _member_declarations(cls) -> tuple[type[DeclaredFamily], ...]:
        """Discovery completes the declarations: each rule's observation comparison
        compiles then, so a compared field with no codec owner refuses before any
        heartbeat keys an observation."""
        members = super()._member_declarations()
        for member in members:
            cast(type[StandingRule], member).compiled_comparison()
        return members

    @classmethod
    @cache
    def compiled_comparison(cls) -> None:
        FieldCodec.compile_comparison(cls)
    team_disposes: ClassVar[bool] = True
    urgent: ClassVar[bool] = False
    owner_flagged: ClassVar[bool] = False

    @classmethod
    def disposes_subjects(cls, subjects: tuple[HeartbeatSubject, ...]) -> bool:
        """A teammate verb can dispose of this rule only through its declared subjects."""
        return cls.team_disposes and bool(subjects)

    @classmethod
    def cleared_by(cls, verb: type[ClearingVerb]) -> None:
        """A verb declaring that it clears this rule joins the action its line prints."""
        cls.clearing_verbs = (*vars(cls).get("clearing_verbs", ()), verb)

    def command_values(self) -> Mapping[str, str]:
        """The verb arguments this line knows, by argument name."""
        return {}

    def review_allowed(self, verb: type[ClearingVerb]) -> bool:
        """Ordinary lines retain every verb that declares it clears them."""
        return True

    def undecided(self, store: TodoStore) -> bool:
        """Whether no recorded decision answers this line; only a head verdict can."""
        return True

    def clearing_actions(self) -> tuple[type[ClearingVerb], ...]:
        """The declared actions applicable to this rule, before presentation compacts them."""
        verbs = dict.fromkeys(member for parent in type(self).__mro__
                              for family in vars(parent).get("clearing_verbs", ())
                              for member in family.members_with(family))
        return tuple(verb for verb in verbs if self.review_allowed(verb))

    def clearing_commands(self) -> str:
        """The applicable verbs, with arguments supplied by this line's observed owners."""
        values = self.command_values()
        # Verbs of one tool taking the same arguments print once: `judge ok|back --pr 3 --head H`,
        # not the same arguments once per verb.
        alike: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for verb in self.clearing_actions():
            for command in verb.commands(values):
                tool, name, *arguments = command.split(" ")
                alike.setdefault((tool, tuple(arguments)), []).append(name)
        return " or ".join(f"`{' '.join((tool, '|'.join(names), *arguments))}`"
                           for (tool, arguments), names in alike.items())

    def attention(self) -> bool:
        return True

    @classmethod
    def alarm_pings[P](cls, ping: P) -> tuple[P, ...]:
        return (ping,)

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        """What a recorded action must touch to dispose of this rule's heartbeat line; a rule
        about nothing a verb can act on clears only when its condition does."""
        return ()

    def named_threads(self) -> tuple[ThreadIncarnation, ...]:
        """The threads this line waits on or is about, which may differ from what disposes of
        it: a row naming only stopped threads waits below the board, not on it."""
        return tuple(dict.fromkeys(
            thread for subject in self.subjects() for thread in subject.threads()))

    def rejected_line(self, error: RejectedEnforcementError) -> str:
        return f"Not applied ({error.explanation()}): {self.line()}"

    def enforce(self, comms: Comms) -> None:
        """What Core does itself when this rule breaks; a rule that only reports does nothing."""

    def redecided(self, comms: Comms, current: Todo) -> tuple[StandingRule, ...]:
        """Rules without a Todo decision cannot refresh one."""
        raise StaleTodoDecision(current)

    @staticmethod
    def enforce_all(
        comms: Comms,
        rules: Iterable[StandingRule],
    ) -> Mapping[StandingRule, RejectedEnforcementError]:
        rejected: dict[StandingRule, RejectedEnforcementError] = {}
        for rule in rules:
            try:
                try:
                    rule.enforce(comms)
                except TodoConflict as conflict:
                    fresh = rule.redecided(comms, conflict.current)
                    try:
                        for decided in fresh:
                            decided.enforce(comms)
                    except TodoConflict as changed:
                        raise RepeatedTodoChange(conflict.current, changed.current) from changed
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
        """The nearest class declaring FamilyScan owns the scan its members share; any other
        rule scans independently."""
        return next((owner for owner in cls.__mro__ if FamilyScan in owner.__bases__), cls)

    @classmethod
    def scan_owners(cls) -> tuple[type[StandingRule], ...]:
        return tuple(dict.fromkeys(rule.scan_owner() for rule in cls.members_with(cls)))

    @classmethod
    def prepare(cls, comms: Comms, holds: HeldOrders,
                pulls: ProgressRead) -> None:
        """Rules acquire nothing unless their scan owner declares an input effect."""

    @classmethod
    def prepare_all(cls, comms: Comms, holds: HeldOrders,
                    pulls: ProgressRead) -> None:
        for owner in cls.scan_owners():
            owner.prepare(comms, holds, pulls)

    @classmethod
    def broken(cls, comms: Comms, after: float = 0.0, *,
               observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[StandingRule, ...]:
        """Read each owner once against the supplied frame and its one progress reader."""
        return tuple(rule for owner in cls.scan_owners()
                     for rule in owner.scan_observed(comms, observed, frame)
                     if rule.attention() and rule.broke_after(after))


@dataclass(frozen=True)
class RetiredStandingRule(DefaultExternalName, StandingRule):
    """The rule a delivered heartbeat notice names after its declaration was deleted. Its line
    is history: it scans nothing, is never urgent, and no verb disposes of it, so it is never
    on the board. Deleting a rule therefore never makes a stored notice undecodable."""

    team_disposes = False

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return ()

    def line(self) -> str:
        return "A standing rule deleted since this notice was recorded."

    def broke_after(self, after: float) -> bool:
        return False


class FamilyScan(StandingRule, declared_name=None):
    """The class naming this among its bases owns one scan its members share: a member its
    scan selects is never scanned a second time."""


class CoreActsStandingRule(StandingRule, declared_name=None):
    """Core's own enforce acts on this line, so no teammate verb disposes of it and it locks none."""

    team_disposes = False


class IndependentStandingRule(StandingRule, declared_name=None):
    """A rule whose source read does not consume the unit observation frame."""

    @classmethod
    @abstractmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]: ...

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.violations(comms)


class UndatedStandingRule(StandingRule, declared_name=None):
    """A rule that cannot say when its condition began: it is broken on every read."""

    def broke_after(self, after: float) -> bool:
        return True


@dataclass(frozen=True)
class StorageViolation(UndatedStandingRule, FamilyScan, declared_name=None):
    """One family scan shares each held unit's storage snapshot between its violations."""

    storage: UnitStorage

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        # Storage reads the filesystem, not the git source an unreadable card failed on:
        # a unit whose declared home is gone is that card and a missing home.
        held = frame.holders
        return cls.for_teammates(comms, (state.unit for state in observed if state.unit in held),
                             frame.pulls)

    @classmethod
    def for_teammates(cls, comms: Comms, units: Iterable[ThreadIncarnation],
                  reads: ProgressRead) -> tuple[Self, ...]:
        registry = comms.registry
        snapshot = registry.snapshot()
        members = cls.members_with(cls)
        found: list[Self] = []
        for unit in sorted(set(units), key=lambda unit: unit.name):
            thread = snapshot.require(unit.name)
            if thread.incarnation != unit:
                raise ValueError("Observed teammate incarnation changed")
            storage = UnitStorage.read(registry.storage_rule, thread, reads)
            found.extend(member(storage) for member in members if member.violated(storage))
        return tuple(found)

    @classmethod
    @abstractmethod
    def violated(cls, storage: UnitStorage) -> bool: ...


class StorageHomeMissing(StorageViolation):
    @classmethod
    def violated(cls, storage: UnitStorage) -> bool:
        return storage.missing_home

    def line(self) -> str:
        return (f'{self.storage.unit.name} declared home {self.storage.home.path} is missing.'
                f' who deleted it? -> restore the home checkout')


@dataclass(frozen=True)
class LostTools(UndatedStandingRule):
    thread: ThreadIncarnation
    changes: tuple[str, ...]

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        found = []
        for unit in observed:
            thread = unit.source.thread
            changes = thread.lost_tools()
            if changes:
                found.append(cls(thread=thread.incarnation, changes=changes))
        return tuple(found)

    def line(self) -> str:
        return (f"RESTORE {self.thread.name} lost tools: {'; '.join(self.changes)}."
                " who took them, and why? -> restore its required tools")


@dataclass(frozen=True)
class HolderBranch:
    """A holder's branch difference; two of them may write the same files in parallel."""

    todo: Todo
    holder: ThreadIncarnation
    difference: BranchDifference

    @staticmethod
    def touching[Branch: HolderBranch](
        branches: Sequence[Branch],
    ) -> Iterator[tuple[Branch, Branch]]:
        """Each pair of branches writing a common file of one repository, once. Indexing by
        (repository, file) selects them; any other pair cannot conflict, and a pair on one
        branch or under one holder that does conflict is still a conflict to resolve."""
        sharing: dict[tuple[RepositoryName, str], list[int]] = {}
        for position, branch in enumerate(branches):
            for path in branch.difference.files:
                sharing.setdefault((branch.difference.repository, path), []).append(position)
        pairs = dict.fromkeys(pair for group in sharing.values() for pair in combinations(group, 2))
        return ((branches[left], branches[right]) for left, right in pairs)


@dataclass(frozen=True)
class PreparedBranch(HolderBranch):
    """One held order's captured branch difference, before the cut, in its repository."""

    repository: Path


@dataclass(frozen=True)
class HeldBranch(HolderBranch):
    """One order still writing source, joined to its own allocated checkout. Only the branch
    difference and publication are captured from that worktree before observation; its unit
    may retire it mid-frame. Later PR comparisons use the order's repository, which outlives
    the worktree, never its possibly different local branch/upstream configuration."""

    worktree: Path
    repository: PresentCheckout = field(compare=False, repr=False)
    source: TeammateEvidence = field(compare=False, repr=False)
    queued: bool = field(compare=False)

    @property
    def problem(self) -> str:
        return self.todo.id


@dataclass(frozen=True)
class HolderLine(StandingRule, declared_name=None):
    """A rule about one holder's order: a recorded action reaching the holder disposes its line."""

    holder: ThreadIncarnation

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (TeammateSubject(self.holder),)


@dataclass(frozen=True)
class HolderProblemLine(HolderLine, declared_name=None):
    """A rule about the board problem a holder is working."""

    problem: str


@dataclass(frozen=True)
class HeldOrderLine(HolderProblemLine, declared_name=None):
    """A rule about the order a holder holds for its problem."""

    order: OrderId


class HeadLine(StandingRule, declared_name=None):
    """A rule about one pull request head: a verdict recorded on that head disposes its line."""

    request: PullRequestHead

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (HeadSubject(self.request),)


class HeldBranchRule(HolderProblemLine, UndatedStandingRule, FamilyScan, declared_name=None):
    """Branch rules share the heartbeat's existing progress frame and one scan."""

    @classmethod
    def prepare(cls, comms: Comms, holds: HeldOrders,
                pulls: ProgressRead) -> None:
        storage = comms.registry.storage_rule
        prepared: list[PreparedBranch] = []
        for held in holds.live:
            todo = held.todo
            directory = todo.order.checked().check.directory
            try:
                reader = pulls.checkout(directory).branches
                for pull in pulls.open_for(todo):
                    pull.prepare_comparison(reader)
                path = storage.order_checkout(storage.roots_of(held.holder), todo.id)
                if path.is_dir():
                    pulls.checkout(path).branches.prepare_upstreams()
                    prepared.append(PreparedBranch(
                        todo=todo, holder=held.holder.incarnation,
                        difference=pulls.branch_difference(path),
                        repository=pulls.checkout(directory).path))
            except (OSError, GitCommandError,
                    WorkBranchError, TimeoutExpired) as error:
                pulls.unread((todo,), error)
        for member in cls.members_with(cls):
            member.prepare_captured(tuple(prepared), pulls)

    @classmethod
    def prepare_captured(cls, prepared: tuple[PreparedBranch, ...], pulls: ProgressRead) -> None:
        """A member that needs more than each branch's own difference reads it here, before
        the cut; its scan consumes only what this captured."""

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        storage = comms.registry.storage_rule
        captured = frame.pulls.captured_branch_paths
        branches = []
        for state in observed:
            merges = state.source.merges()
            writing = frozenset(todo.id for todo in state.source.todos
                                if todo.order.writes_source(todo, merges))
            for unit in state.readable_units():
                for held in frame.live:
                    todo = held.todo
                    if held.holder.incarnation == unit and todo.id in writing:
                        path = storage.order_checkout(storage.roots_of(held.holder), todo.id)
                        if path.resolve() in captured:
                            branches.append(HeldBranch(
                                todo, unit, frame.pulls.require_branch_difference(path), path,
                                frame.pulls.checkout(todo.order.checked().check.directory),
                                state.source,
                                any(request.queued for request in frame.pulls.open_for(todo)),
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
class RepositoryBranchRule(HeldBranchRule, declared_name=None):
    """A rule about one named branch of a held order's repository."""

    repository: RepositoryName
    branch: str


@dataclass(frozen=True)
class HeldOrderBranchRule(HeldOrderLine, HeldBranchRule, declared_name=None):
    """A branch rule that names the held order it is about."""


@dataclass(frozen=True)
class BranchBehindMain(CoreActsStandingRule, RepositoryBranchRule):
    """Outstanding branch changes need a new base when main passes them. Core tells
    the holder directly; an empty historical checkout owes no new publication."""

    behind: int = field(compare=False)

    def enforce(self, comms: Comms) -> None:
        # Retain the registry's alias/birth decision through publication. A departed
        # holder belongs to HolderGone, never to a new participant reusing its name.
        # The notice is sent after the snapshot's shared lock is released.
        with comms.registry.store.current_snapshot() as cut:
            names = tuple(holder.name
                          for holder in cut.snapshot.threads_for(self.holder.require_recorded())
                          if cut.snapshot.status(holder.name).publish_heartbeat(
                              lambda: True, False))
        for name in names:
            OrderNotice().publish(comms.messaging, ThreadTarget(name), (
                f"{self.branch} is {self.behind} commits behind main. Rebase your branch"
                " onto origin/main now, retain main's moves and deletions, resolve every"
                " conflict yourself, push with --force-with-lease, and keep working; a"
                " stale tree turns your next push into everyone's conflict."))

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(cls(problem=held.problem, holder=held.holder,
                         repository=held.difference.repository,
                         branch=held.difference.branch, behind=held.difference.behind)
                     for held in branches if held.difference.needs_rebase and not held.queued)

    def line(self) -> str:
        return (f"REBASE {self.repository.to_wire()}:{self.branch} ({self.holder.name} {self.problem}) is"
                f" {self.behind} commits behind main. building on a stale tree"
                " for fun? -> rebase your branch onto origin/main")


@dataclass(frozen=True)
class BranchGrowth(RepositoryBranchRule):
    """A held branch's source growth needs an explanation in its verdict."""

    growth: SourceGrowth = field(compare=False)

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(
            cls(problem=held.problem, holder=held.holder,
                repository=held.difference.repository,
                branch=held.difference.branch, growth=held.difference.growth)
            for held in branches if held.difference.growth.exceeds
        )

    def line(self) -> str:
        return (
            f"SHRINK {self.repository.to_wire()}:{self.branch} ({self.holder.name} {self.problem}) grows"
            f" src by {self.growth.total} code lines, limit {self.growth.limit}. is all of it"
            " necessary, or is it slop? -> the verdict states why the growth is necessary, or the"
            " slop is deleted"
        )


@dataclass(frozen=True)
class UnparsedBranch(RepositoryBranchRule):
    """A held branch pushed src that Python cannot parse; its growth cannot be counted."""

    unparsed: tuple[UnparsedSource, ...] = field(compare=False)

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(
            cls(problem=held.problem, holder=held.holder,
                repository=held.difference.repository,
                branch=held.difference.branch, unparsed=held.difference.growth.unparsed)
            for held in branches if held.difference.growth.unparsed
        )

    def line(self) -> str:
        return (
            f"UNPARSED {self.repository.to_wire()}:{self.branch} ({self.holder.name} {self.problem})"
            f" pushed src that does not parse: {', '.join(u.listing() for u in self.unparsed)}."
            " conflict markers pushed? -> fix the file and push"
        )


@dataclass(frozen=True)
class ConflictingBranches(RepositoryBranchRule):
    """Two held branches that Git cannot merge: one row per pair, only for a real conflict.

    Two branches touching the same file is ordinary parallel work; the row exists only
    when ``git merge-tree`` of their heads conflicts, and merging one into the other clears
    it, so no row asks a question nothing can answer."""

    other_problem: str
    other_holder: ThreadIncarnation
    other_branch: str
    paths: tuple[str, ...] = field(compare=False)

    @classmethod
    def prepare_captured(cls, prepared: tuple[PreparedBranch, ...], pulls: ProgressRead) -> None:
        for first, second in HolderBranch.touching(prepared):
            pulls.branch_conflicts(first.repository, first.difference.head, second.difference.head)

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(
            cls(problem=first.problem, holder=first.holder, branch=first.difference.branch,
                other_problem=second.problem, other_holder=second.holder,
                other_branch=second.difference.branch, repository=first.difference.repository,
                paths=conflicts)
            for first, second in HolderBranch.touching(branches)
            if (conflicts := first.source.frame.pulls.require_branch_conflicts(
                first.difference.head, second.difference.head))
        )

    def line(self) -> str:
        return (f"CONFLICT {self.holder.name} {self.problem} ({self.branch}) and"
                f" {self.other_holder.name} {self.other_problem} ({self.other_branch}) do not merge"
                f" in {self.repository.to_wire()}: {', '.join(self.paths)}. -> merge one into the"
                " other and resolve")


@dataclass(frozen=True)
class DatedStandingRule(StandingRule, declared_name=None):
    """A rule that says when its condition began: it is broken on the reads after that."""

    @abstractmethod
    def began(self) -> float: ...

    def broke_after(self, after: float) -> bool:
        return self.began() >= after


@dataclass(frozen=True)
class OwedAnswer(IndependentStandingRule, FamilyScan, declared_name=None):
    """A question or delivery awaiting the teammate; each member owns what clears it
    and which actions it prevents while it waits."""

    message: Message

    def questions_for(self, unit: ThreadIncarnation) -> tuple[Message, ...]:
        """Only a unit's question belongs on that unit's observation."""
        return ()

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (AnswerSubject(self.message.message_id),)

    @property
    def cited(self) -> str:
        """The owed message as the heartbeat and the gate name it."""
        return f"{self.message.seq}:{self.message.message_id}"

    @classmethod
    @abstractmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], debtor: Thread,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        """This member's open debts, inside their window or past it, on one bus cut."""

    @abstractmethod
    def clearing_action(self) -> str:
        """The action that clears this member's debt, printed by the refusing verb."""

    def due(self, now: float) -> bool:
        return True

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        """Every live teammate's due debts: the team board shows them all."""
        now = time.time()
        return tuple(rule
                     for teammate in TeamService.teammates(comms.registry.snapshot(), comms.goals)
                     for rule in cls.opened(comms, teammate.incarnation) if rule.due(now))

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        now = time.time()
        return tuple(rule for rule in frame.answers if isinstance(rule, cls) and rule.due(now))

    @classmethod
    def opened(cls, comms: Comms, owner: ThreadIncarnation) -> tuple[Self, ...]:
        return cls.read_scan(comms, owner, HeldOrders.read(comms))

    @classmethod
    def read_scan(cls, comms: Comms, owner: ThreadIncarnation, scan: HeldOrders) -> tuple[Self, ...]:
        # Capture committed order/directive references before the bus cut.
        cut = comms.registry.store.snapshot_with_authority()
        snapshot = cut.snapshot
        thread = snapshot.require(owner.name)
        if thread.incarnation != owner:
            raise RelationViolationError(
                f"Answer owner {owner.name} changed incarnation; read the current owner before retrying")
        # Maintaining another owner's work does not make its private directives ours.
        requests = comms.goals.todos.open_requests(thread.acting_identity)
        held = scan.holders
        with comms.bus.bus.reading() as source:
            opened = tuple(rule for member in cls.members_with(cls)
                           for rule in member.read_open(
                               source, snapshot, held, thread, requests))
        questions: dict[str, tuple[Thread, dict[MessageReference, None]]] = {}
        for rule in opened:
            answerer = rule.session_answerer(thread)
            _, references = questions.setdefault(answerer.name, (answerer, {}))
            references.update(dict.fromkeys(rule.session_questions()))
        answered = frozenset().union(*(
            comms.transcripts.answered_inputs(answerer, tuple(references), cut.privacy)
            for answerer, references in questions.values()
        ))
        return tuple(rule for rule in opened if rule.undecided(comms.goals.todos)
                     and not rule.answered_in_session(answered))

    def session_questions(self) -> tuple[MessageReference, ...]:
        """Ordinary owed answers still require their existing bus answer relation."""
        return ()

    def session_answerer(self, reader: Thread) -> Thread:
        """Whose transcript answers this debt's session questions: by default its reader's."""
        return reader

    def answered_in_session(self, answered: frozenset[MessageReference]) -> bool:
        questions = self.session_questions()
        return bool(questions) and all(question in answered for question in questions)

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        """Whether an action naming these open messages answers this one."""
        return any(message.message_id == self.message.message_id for message in named)

    def blocks(self, reached: frozenset[str], action: ClearingVerb,
               answered: tuple[OwedAnswer, ...]) -> bool:
        """An owed message refuses only an action that answers nothing: an answer to any open
        message is progress and is never refused, so the others it leaves owed only warn."""
        return not answered

    def answered_in(self) -> tuple[str, ...]:
        """The threads a reply must reach to answer this debt by naming it; none when a reply
        cannot clear it."""
        return ()

    def asked_by(self, reached: frozenset[str]) -> bool:
        """Whether one of these threads asked it, so a reply reaching them answers it unnamed."""
        return False

    def misaddressed(self, named: tuple[Message, ...], reached: frozenset[str]) -> str:
        """Why naming this debt in an action that reaches only these threads would record an
        answer its asker never receives; empty when it would not."""
        return ""


@dataclass(frozen=True)
class TeamInputs(HeldOrders):
    """The live hold cut and acquired inputs that observation must not reopen."""

    pulls: ProgressRead
    plan: PlanReading

    @classmethod
    def prepare(cls, comms: Comms, project: RegisteredTeamProject, pulls: ProgressRead,
                plans: PlanReadings) -> Self:
        holds = HeldOrders.read(comms)
        pulls.register(holds.todos)
        repositories = project.repository_names()
        pulls.read_open(repositories)
        repository = Repo.declaring(project.repos, PlanPhase.__module__)
        plan = plans.prepare_reading(
            repository,
            comms.goals.todos.board_history(),
            tuple(phase.phase_evidence() for phase in PlanPhase.members_with(PlanPhase)),
            history=pulls.checkout(repository.checkout).branches.history,
        )
        StandingRule.prepare_all(comms, holds, pulls)
        return cls(holds.live, holds.orphaned, pulls, plan)


@dataclass(frozen=True)
class HeartbeatFrame(TeamInputs):
    """One beat's observed answers, sharing its already prepared hold/forge/plan inputs."""

    answers: tuple[OwedAnswer, ...]
    closed: ClosedHeadReading
    head_reviews: Mapping[PullRequestHead, tuple[HeadReviewRecord, ...]]
    workplaces: Mapping[ThreadIncarnation, EditWorkplaces]
    review_timers: tuple[tuple[Todo, RecordedEventTime], ...]
    recent: ClassVar[LastTurns] = LastTurns(2)

    @cached_property
    def _turns(self) -> dict[ThreadIncarnation, tuple[TurnDigest, ...]]:
        return {}

    @cached_property
    def _edit_counts(self) -> dict[str, int]:
        return {}

    def turns_for(self, comms: Comms, thread: Thread) -> tuple[TurnDigest, ...]:
        """One original turn read covering this holder's review cuts and recent window."""
        identity = thread.incarnation
        if identity not in self._turns:
            since = min((comms.goals.todos.unjudged_since(todo)
                         for todo in self.holders.get(identity, ())), default=time.time())
            self._turns[identity] = TurnDigestRead(
                comms.root, thread, RecentTurnsSince(since, recent=self.recent)).read().turns
        return self._turns[identity]

    def recent_turns(self, comms: Comms, thread: Thread) -> tuple[TurnDigest, ...]:
        return self.recent.take(self.turns_for(comms, thread))

    def edit_counts(self, comms: Comms) -> Mapping[str, int]:
        """Count through each source's own query; retain the answer within this frame."""
        for read in OrderEditCountRead.judgeable(
                comms.root, self.live, self.pulls.progress(self.todos, comms.registry),
                self.workplaces):
            if read.todo.id not in self._edit_counts:
                self._edit_counts[read.todo.id] = read.read()
        for todo in self.todos:
            self._edit_counts.setdefault(todo.id, 0)
        return self._edit_counts

    @classmethod
    def capture(cls, comms: Comms, inputs: TeamInputs, plans: PlanReadings) -> Self:
        """One team board: every teammate's owed answers, never one lead's."""
        snapshot = comms.registry.snapshot()
        project = snapshot.team_project.require()
        with comms.goals.todos.snapshot() as db:
            closed = StoredClosedHead.current(db, project.repository_names())
            reviews = StoredHeadReview.head_reviews_among(
                db, (head.request for head in closed.lost))
        plan = plans.observe_reading(inputs.plan,
            tuple(phase.phase_evidence() for phase in PlanPhase.members_with(PlanPhase)))
        return cls(
            inputs.live,
            inputs.orphaned,
            inputs.pulls,
            plan,
            tuple({answer.observation_key(): answer
                   for teammate in TeamService.teammates(snapshot, comms.goals)
                   for answer in OwedAnswer.read_scan(comms, teammate.incarnation, inputs)
                   }.values()),
            closed,
            reviews,
            EditWorkplaces.capture(comms.goals, snapshot, inputs.holders, inputs.pulls),
            TodoStore.timers_up_in(inputs.todos, time.time(), inputs.pulls.progress(inputs.todos, comms.registry)),
        )


@dataclass(frozen=True)
class ReasonedStandingRule(StandingRule, declared_name=None):
    """A rule broken at most once, saying why in its declared line."""

    why: str = field(compare=False)
    text: ClassVar[str]

    @classmethod
    @abstractmethod
    def reason(cls, comms: Comms, frame: HeartbeatFrame) -> str | None: ...

    @classmethod
    def found(cls, why: str, comms: Comms,
              observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> Self:
        return cls(why=why)

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        why = cls.reason(comms, frame)
        return (cls.found(why, comms, observed, frame),) if why else ()

    def line(self) -> str:
        return self.text.format(why=self.why)


@dataclass(frozen=True)
class HeldOrderDue(HeldOrderLine, DatedStandingRule, declared_name=None):
    """Each held order supplies one dated condition; the rule owns its wording."""

    since: EventTime

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        """Conditions read the frame's own progress facts, never a second forge read."""
        return tuple(cls(problem=todo.id, holder=todo.held().owner_identity,
                         order=todo.order.checked().id, since=due)
                     for todo, due in cls.conditions(comms, frame))

    @classmethod
    @abstractmethod
    def conditions(
        cls, comms: Comms, frame: HeartbeatFrame,
    ) -> Iterable[tuple[Todo, EventTime]]: ...

    def began(self) -> float:
        return self.since.recorded_at("An order alarm")


class HeadReviewRule(StandingRule, declared_name=None):
    """A head verdict can answer this rule's review request."""

    request: PullRequestHead

    @staticmethod
    def decided(store: TodoStore, request: PullRequestHead) -> bool:
        """A recorded verdict on exactly this head answers it; a reviewing mark still owes one."""
        return any(review.verdict.decides() for review in StoredHeadReview.head_reviews(store, request))

    def undecided(self, store: TodoStore) -> bool:
        return not self.decided(store, self.request)

    def review_allowed(self, verb: type[ClearingVerb]) -> bool:
        return verb.source_review_hint()


@dataclass(frozen=True)
class HeadReviewCommands(HeadReviewRule, declared_name=None):
    """A review head supplies the common arguments accepted by the judge."""

    request: PullRequestHead

    def command_values(self) -> Mapping[str, str]:
        return {"repo": self.request.repo.to_wire(), "pr": str(self.request.pr),
                "head": self.request.head}


@dataclass(frozen=True)
class OrderHeadReviewCommands(HeadReviewCommands, declared_name=None):
    """A linked review carries exactly the order whose verdict permissions it prints."""

    todo: Todo = field(kw_only=True, compare=False)

    def review_allowed(self, verb: type[ClearingVerb]) -> bool:
        return (super().review_allowed(verb)
                and verb.review_allowed(self.todo.order.checked().review_verdicts()))

    def command_values(self) -> Mapping[str, str]:
        return {**super().command_values(), "problem": self.todo.id}


class ReviewTimerUp(HeldOrderDue, FamilyScan):
    """The timer owns the request and wake; the unit's ReviewDue row owns its display."""

    @classmethod
    def alarm_pings[P](cls, ping: P) -> tuple[P, ...]:
        return ()

    @classmethod
    def conditions(cls, comms: Comms, frame: HeartbeatFrame) -> Iterable[tuple[Todo, EventTime]]:
        return frame.review_timers

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[ReviewTimerUp, ...]:
        rules: list[ReviewTimerUp] = []
        for todo, due in cls.conditions(comms, frame):
            for request in frame.pulls.open_for(todo)[:1]:
                head = PullRequestHead(todo.repo, request.number, request.head)
                if todo.order.holds_head(head):
                    rules.append(HeadReviewTimerUp(
                        problem=todo.id, holder=todo.held().owner_identity,
                        order=todo.order.checked().id, since=due, request=head, todo=todo))
                    break
            else:
                rules.append(ReviewTimerUp(problem=todo.id, holder=todo.held().owner_identity,
                                          order=todo.order.checked().id, since=due))
        return tuple(rules)

    def line(self) -> str:
        return (f"READ {self.holder.name}'s timer on {self.problem} is up ({self.since.clock()}, order"
                f" {self.order}). what did it actually do? -> read its diff and judge it")


@dataclass(frozen=True)
class HeadReviewTimerUp(OrderHeadReviewCommands, ReviewTimerUp):
    """A timer with a current PR head offers only that order's accepted verdicts."""

    def line(self) -> str:
        return f"{super().line()} -> {self.clearing_commands()}"


@dataclass(frozen=True)
class DeliveredHeadReview(HeldOrderLine, DatedStandingRule):
    """A recorded delivery waits on a review of its current ready PR head."""

    repo: RepositoryName
    pr: int
    head: str
    due_at: float
    minutes: ClassVar[float] = 5

    @classmethod
    def for_head(
        cls, problem: str, holder: ThreadIncarnation, order: OrderId, claim: OrderClaim,
        request: PullRequest, now: float,
    ) -> tuple[Self, ...]:
        due_at = claim.message.timestamp + cls.minutes * 60
        return tuple(cls(problem=problem, holder=holder, order=order,
                         repo=claim.pull_request.repo, pr=request.number,
                         head=request.head, due_at=due)
                     for due in request.head_review_due(now, due_at))

    @classmethod
    def scan_observed(
        cls, comms: Comms, observed: Sequence[ObservedTeammate], frame: HeartbeatFrame,
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

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (*super().subjects(), HeadSubject(PullRequestHead(self.repo, self.pr, self.head)))

    def line(self) -> str:
        return (f"JUDGE {self.holder.name} delivered {self.repo.to_wire()}#{self.pr} {self.head[:9]}"
                f" ({self.problem}, order {self.order}). you read it yet?"
                f" -> judge ok/back --pr {self.pr} --head {self.head}")


@dataclass(frozen=True)
class UnreviewedHead(HeadLine, HeadReviewCommands, DatedStandingRule, declared_name=None):
    """One ready head waiting too long for a decision; a reviewing mark is not one."""
    due_at: float
    seconds: ClassVar[int] = 10 * 60

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.scan_reviews(comms.goals, frame.pulls)

    @classmethod
    def scan_reviews(cls, goals: Goals, pulls: ProgressRead) -> tuple[Self, ...]:
        now = time.time()
        snapshot = goals.registry.snapshot()
        todos = goals.todos.board()
        return tuple(
            rule
            for repo in snapshot.team_project.repository_names()
            for row in pulls.open(repo)
            for due in row.review_due(now, cls.seconds)
            for request in (PullRequestHead(repo, row.number, row.head),)
            if not cls.decided(goals.todos, request)
            for rule in cls.for_head(request, due, todos)
        )

    @classmethod
    @abstractmethod
    def for_head(cls, request: PullRequestHead, due: float, todos: Sequence[Todo]) -> tuple[Self, ...]:
        return (cls(request=request, due_at=due),)

    def began(self) -> float:
        return self.due_at

    def line(self) -> str:
        return (f"JUDGE {self.request.repo.to_wire()}#{self.request.pr} {self.request.head[:9]} is sitting"
                f" there unjudged. you read it yet?"
                f" -> {self.clearing_commands()}")


@dataclass(frozen=True)
class LinkedUnreviewedHead(OrderHeadReviewCommands, UnreviewedHead):
    """An overdue linked head remains visible without preventing unrelated recovery work."""

    @classmethod
    def for_head(cls, request: PullRequestHead, due: float, todos: Sequence[Todo]) -> tuple[Self, ...]:
        return tuple(cls(request=request, due_at=due, todo=todo)
                     for todo in HeadReviewOrder.holding(todos, request))


@dataclass(frozen=True)
class OrderlessUnreviewedHead(UnreviewedHead):
    """A head no held order links: a board line that refuses nobody's orders."""

    @classmethod
    def for_head(cls, request: PullRequestHead, due: float, todos: Sequence[Todo]) -> tuple[Self, ...]:
        return () if HeadReviewOrder.holding(todos, request) else super().for_head(request, due, todos)


@dataclass(frozen=True)
class StuckMerge(HeadLine, DatedStandingRule):
    """An armed head GitHub will not merge: approved, queued and going nowhere. One line per
    head, naming every block on it."""
    request: PullRequestHead
    blocks: tuple[MergeBlock, ...] = field(compare=False)
    holders: tuple[ThreadIncarnation, ...] = field(default=(), compare=False)

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.scan_pulls(comms.goals, frame.pulls)

    @classmethod
    def scan_pulls(cls, goals: Goals, pulls: ProgressRead) -> tuple[Self, ...]:
        return tuple(
            cls(request=PullRequestHead(repo, row.number, row.head), blocks=blocks,
                holders=tuple(dict.fromkeys((
                    *(held.owner_identity
                      for todo in pulls.linked(repo).get(row.number, ())
                      for held in todo.current_holder.current_assignments()),
                    *pulls.working_on(goals, repo, row)))))
            for repo in goals.registry.snapshot().team_project.repository_names()
            for row in pulls.open(repo)
            for blocks in (row.stuck(),) if blocks
        )

    def began(self) -> float:
        return min(block.since for block in self.blocks)

    def line(self) -> str:
        return (f"UNSTICK {self.request.repo.to_wire()}#{self.request.pr} {self.request.head[:9]} has"
                f" auto-merge armed but {'; '.join(block.what() for block in self.blocks)}. who's fixing it?"
                f" -> {'; '.join(block.remedy() for block in self.blocks)}"
                + (f" at {', '.join(holder.name for holder in self.holders)}" if self.holders else ""))


@dataclass(frozen=True)
class IdlePullRequest(HeadLine, DatedStandingRule):
    """An open head, draft or ready, going nowhere outside the merge queue: unmoved for the
    idle window, or blocked by a conflict or a red required check. Armed heads are
    StuckMerge's; every other open request linked to one of this teammate's orders is a
    board item. Another teammate's requests share the repositories, never this board; a
    request no open order links is OrderlessPullRequest's."""
    request: PullRequestHead
    holders: tuple[ThreadIncarnation, ...] = field(compare=False)
    blocks: tuple[MergeBlock, ...] = field(compare=False)
    moved: float = field(compare=False)
    seconds: ClassVar[int] = 30 * 60
    tag: ClassVar[str] = "IDLE"
    unheld: ClassVar[str] = "no live holder"
    unowned: ClassVar[str] = "land or supersede it"

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        ordered = comms.goals.todos.board()
        return cls.scan_pulls(comms.goals, frame.pulls, frame.todos, ordered)

    @classmethod
    def scan_pulls(cls, goals: Goals, pulls: ProgressRead,
                   held: Sequence[Todo], ordered: Sequence[Todo]) -> tuple[Self, ...]:
        """Holders are the live holds whose orders link the request, as the HUD cards read;
        the teammate's orders' links own which requests are its fleet's."""
        now = time.time()
        repos = goals.registry.snapshot().team_project.repository_names()
        linked = {repo: pulls.linked_among(ordered, repo) for repo in repos}
        holders: dict[tuple[RepositoryName, int], tuple[ThreadIncarnation, ...]] = {}
        for todo in held:
            for request in pulls.open_for(todo):
                key = (todo.repo, request.number)
                holders[key] = (*holders.get(key, ()), todo.held().owner_identity)
        found = (
            cls(request=PullRequestHead(repo, row.number, row.head),
                holders=holders.get((repo, row.number), ()),
                blocks=row.merge_blocks, moved=row.reviewable_at)
            for repo in repos
            for row in pulls.open(repo) if row.number in linked[repo] and not row.armed_times
        )
        return tuple(rule for rule in found if rule.began() <= now)

    def began(self) -> float:
        return min((*(block.since for block in self.blocks), self.moved + self.seconds))

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        """A verdict on the head or an order to its holder disposes of the line."""
        return (*super().subjects(), *(TeammateSubject(holder) for holder in self.holders))

    def unheld_actions(self, remedies: tuple[str, ...]) -> tuple[str, ...]:
        """With nobody holding the branch, its remedies say what to do; with none, the default."""
        return remedies or (self.unowned,)

    def line(self) -> str:
        held = ", ".join(holder.name for holder in self.holders) or self.unheld
        why = "; ".join(block.what() for block in self.blocks) or "head unmoved"
        remedies = tuple(block.remedy() for block in self.blocks)
        actions = ((*remedies, *(f"order {holder.name}" for holder in self.holders))
                   if self.holders else self.unheld_actions(remedies))
        return (f"{self.tag} {self.request.repo.to_wire()}#{self.request.pr} {self.request.head[:9]} ({held}):"
                f" {why}, unchanged since {RecordedEventTime(self.moved).stamp()}. who owns it, or are we"
                f" pretending it doesn't exist? -> {'; '.join(actions)}")


@dataclass(frozen=True)
class OrderlessPullRequest(IdlePullRequest):
    """An open head that no open order links, from any teammate: work done outside the order
    system, which no holder line would ever show. Its blocks and the threads working in a
    checkout of its branch go on the board, so a red required check reaches whoever is working
    on it. A head another teammate's orders link stays on that teammate's board, and an
    armed head is StuckMerge's."""

    tag: ClassVar[str] = "UNORDERED"
    unheld: ClassVar[str] = "no order, and no registered thread has its branch checked out"
    unowned: ClassVar[str] = "order its author or close it with a reason"

    def unheld_actions(self, remedies: tuple[str, ...]) -> tuple[str, ...]:
        """A remedy "at its owner" names no one here: work outside the orders that nobody
        holds needs its author ordered first."""
        return (self.unowned,)

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.scan_orderless(comms.goals, frame.pulls)

    @classmethod
    def scan_orderless(cls, goals: Goals, pulls: ProgressRead) -> tuple[Self, ...]:
        now = time.time()
        orders = goals.todos.board()
        found = (
            cls(request=PullRequestHead(repo, row.number, row.head),
                holders=pulls.working_on(goals, repo, row),
                blocks=row.merge_blocks, moved=row.reviewable_at)
            for repo in goals.registry.snapshot().team_project.repository_names()
            for linked in (pulls.linked_among(orders, repo),)
            for row in pulls.open(repo) if row.number not in linked and not row.armed_times
        )
        return tuple(rule for rule in found if rule.began() <= now)


@dataclass(frozen=True)
class ChurningPullRequest(IdlePullRequest):
    """An open head whose required checks are red a day after the request opened, however
    often it moves. Each push restarts the idle window, so a request that keeps changing and
    never goes green looked busy forever (2026-11-03: Toad #1389 stayed red for four days and
    grew to 140 files while its holder pushed every hour, and no line named it)."""

    opened: float = field(compare=False, kw_only=True)
    seconds: ClassVar[int] = 24 * 60 * 60
    tag: ClassVar[str] = "CHURNING"
    unheld: ClassVar[str] = "nobody has its branch checked out"
    unowned: ClassVar[str] = "land what is green or split it"

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        goals, pulls, now = comms.goals, frame.pulls, time.time()
        found = (
            cls(request=PullRequestHead(repo, row.number, row.head),
                holders=pulls.working_on(goals, repo, row),
                blocks=row.merge_blocks, moved=row.reviewable_at, opened=row.opened_at)
            for repo in goals.registry.snapshot().team_project.repository_names()
            for row in pulls.open(repo) if not row.armed_times
        )
        return tuple(rule for rule in found if rule.began() <= now)

    def began(self) -> float:
        return self.opened + self.seconds if any(block.red() for block in self.blocks) else math.inf

    def line(self) -> str:
        held = ", ".join(holder.name for holder in self.holders) or self.unheld
        red = "; ".join(block.what() for block in self.blocks if block.red())
        return (f"{self.tag} {self.request.repo.to_wire()}#{self.request.pr} {self.request.head[:9]}"
                f" ({held}): opened {RecordedEventTime(self.opened).stamp()} and still red: {red}."
                f" busy isn't landing. -> {self.unowned}")


@dataclass(frozen=True)
class HuntDue(ReasonedStandingRule, DatedStandingRule, FamilyScan):
    """Its scan picks the located member when a hot file is known; the members share it."""

    since: float
    seconds: ClassVar[int] = 900

    @classmethod
    def hunted(cls, comms: Comms) -> float:
        return max((hunt.at.recorded_at("A hunt") for hunt in comms.goals.todos.hunts()), default=0)

    @classmethod
    def reason(cls, comms: Comms, frame: HeartbeatFrame) -> str | None:
        age = time.time() - cls.hunted(comms)
        return f"no hunt in the last {cls.seconds // 60} min" if age > cls.seconds else None

    @classmethod
    def found(cls, why: str, comms: Comms,
              observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> HuntDue:
        since = cls.hunted(comms) + cls.seconds
        readable = {unit for state in observed for unit in state.readable_units()}
        todos = tuple(todo for todo in frame.todos
                      if todo.held().owner_identity in readable)
        return next((LocatedHuntDue(why=why, since=since, where=path)
                     for path in cls.next_hunt(comms, todos)),
                    cls(why=why, since=since))

    @classmethod
    def next_hunt(cls, comms: Comms, todos: Iterable[Todo]) -> tuple[str, ...]:
        """Least recently hunted hot file in the held orders' declared check repositories."""
        hunted: dict[str, float] = {}
        for hunt in comms.goals.todos.hunts():
            hunted[hunt.where.file] = max(hunted.get(hunt.where.file, 0),
                                          hunt.at.recorded_at("A hunt"))
        repos = {todo.repository
                 for todo in todos}
        candidates: list[str] = []
        for repo in repos:
            log = repo.git(repo.checkout, 'log', f'{repo.remote}/main', '--since=7.days',
                           '--name-only', '--format=', '--', repo.source)
            churn = Counter(str((repo.checkout / name).resolve())
                            for name in log.splitlines()
                            if name.endswith('.py') and (repo.checkout / name).is_file())
            candidates.extend(path for path, _ in churn.most_common(15))
        return tuple(sorted(set(candidates), key=lambda path: (hunted.get(path, 0), path)))[:1]

    def line(self) -> str:
        return ("HUNT nothing hot found in any held order's production source. so what's slow?"
                " -> hunt it and order the fix this wake")

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class LocatedHuntDue(HuntDue):
    """The hunt line naming its hot file, selected by HuntDue's scan rather than a second one."""

    where: str

    def line(self) -> str:
        return f"HUNT {self.where} is hot and nobody's on it. why? -> order the fix this wake"


@dataclass(frozen=True)
class DirectiveUnacted(OwedAnswer, DatedStandingRule):
    """An owner directive recorded on an open request, past its deadline with no order since."""

    owner_flagged: ClassVar[bool] = True

    request: OwnerRequestId
    directive: RequestDirective
    answerer: Thread = field(compare=False)

    def session_questions(self) -> tuple[MessageReference, ...]:
        return (self.message.reference,)

    def session_answerer(self, reader: Thread) -> Thread:
        return self.answerer

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], debtor: Thread,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        """The frozen recipient owes the private directive, not every request maintainer.
        Stored request associations cannot substitute a different mailbox or birth.
        """
        return tuple(
            cls(message=original, request=request.id, directive=directive,
                answerer=debtor)
            for request in requests
            for directive, original in request.directive_messages(
                source, debtor.incarnation.require_recorded().lookup)
        )

    def answered_in(self) -> tuple[str, ...]:
        return (self.message.sender,)

    def clearing_action(self) -> str:
        return (f"Order its next step with --answers {self.message.message_id}, reply to "
                f"{self.message.sender} on the bus with it, or mark request {self.request.value} done.")

    def due(self, now: float) -> bool:
        return self.directive.deadline < now

    def began(self) -> float:
        return self.directive.deadline

    def line(self) -> str:
        return (
            f"ACT {self.answerer.name}'s owner directive {self.cited}"
            f" on request {self.request.value} was due"
            f" {self.directive.due_time}. didn't we already have a plan due then?"
            " -> order its next step or mark the request done"
        )


@dataclass(frozen=True)
class ServicePassFailing(FailingPass, IndependentStandingRule, DatedStandingRule, FamilyScan):
    """A Core service watch whose newest pass failed; the connector's log reaches nobody."""

    watch: str
    error: str = field(compare=False)
    failed_at: float = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        return cls.from_passes(ServicePassStore.at(comms.root).read())

    @classmethod
    def from_passes(cls, passes: ServicePasses) -> tuple[Self, ...]:
        return tuple(cls(watch=watch, since=failed.since, error=failed.error,
                         failed_at=failed.failed_at)
                     for watch, failed in sorted(passes.failing.items()))

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return self.failure_line(self.watch)


@dataclass(frozen=True)
class BoardDisagrees(UndatedStandingRule):
    """An open order reads as no commit despite its linked pull request having merged."""

    problem: str
    order: OrderId
    merge: str

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[ObservedTeammate], frame: HeartbeatFrame) -> tuple[Self, ...]:
        progress = frame.pulls.progress(frame.todos, comms.registry)
        return tuple(
            cls(problem=todo.id, order=todo.order.checked().id, merge=merged.sha)
            for todo in frame.todos
            for merged in progress[todo.id].missing_merges(
                ProgressSource.of(todo, comms.registry, frame.pulls),
            )
        )

    def line(self) -> str:
        return (
            f"REPAIR the board reads no commit for {self.problem} order {self.order}, but its linked PR"
            f" merged as {self.merge[:9]}. why guess when git has the answer?"
            " -> repair the progress read this wake"
        )


@dataclass(frozen=True)
class HostMemoryHigh(IndependentStandingRule, UndatedStandingRule):
    """A shared control host which has exceeded its measured regression budget."""

    process: ProcessIdentity
    memory: ResidentMemory = field(compare=False)
    rule: StorageRule = field(compare=False)
    owners: int
    fleet_resident: int = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        try:
            runtime = RootRuntimeStore.at(comms.root).read()
        except FileNotFoundError:
            return ()  # A root which never launched a worker has no host memory.
        process = runtime.host.observed(comms.root)
        if not process.alive():
            return ()
        try:
            identity = process.require()
            memory = identity.memory(comms.registry.storage_rule.proc)
        except ProcessLookupError:
            return ()
        rule = comms.registry.storage_rule
        owners = sum(thread.process_identity == identity
                     for thread in comms.registry.all_threads().values())
        fleet = sum(row.resident for row in ResidentMemory.tree(identity, rule.proc).values())
        exceeded = rule.host_memory_exceeded(memory) or fleet > rule.team_memory_budget(owners)
        return (cls(identity, memory, rule, owners, fleet),) if exceeded else ()

    def line(self) -> str:
        return (
            f"RAM shared Python host {self.process.pid}: "
            f"{self.memory.resident / 2**20:.0f} MiB resident, "
            f"{self.memory.peak / 2**20:.0f} MiB peak / "
            f"{self.rule.host_memory_bytes / 2**20:.0f} MiB host budget; "
            f"whole team {self.fleet_resident / 2**20:.0f} / "
            f"{self.rule.team_memory_budget(self.owners) / 2**20:.0f} MiB "
            f"for {self.owners} owners"
            " -> inspect comms_resources and fix the host's growing owner"
        )


@dataclass(frozen=True)
class MemoryLow(IndependentStandingRule, UndatedStandingRule):
    """Under the memory threshold a cleanup owner is due, told what holds the memory."""

    rule: StorageRule
    memory: MemoryUse = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        rule = comms.registry.storage_rule
        memory = rule.memory()
        return (cls(rule=rule, memory=memory),) if rule.memory_due(memory) else ()

    def line(self) -> str:
        dirs = ", ".join(map(str, self.memory.idle_dirs)) or "none"
        processes = ", ".join(map(str, self.memory.processes)) or "none"
        return (
            f"FREE {self.memory.available_gib:.1f} GiB of memory available (cleanup due under"
            f" {self.rule.memory_gib:g} GiB); pytest temp dirs idle over"
            f" {self.rule.idle_minutes:g} min: {dirs}; test processes: {processes}"
            ". who's cleaning up? -> put a cleanup owner on it now"
        )


@dataclass(frozen=True)
class DiskLow(IndependentStandingRule, UndatedStandingRule, FamilyScan):
    """Under the cleanup threshold a cleanup owner is due; under the floor nothing allocates."""

    rule: StorageRule
    free_gib: float = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms) -> tuple[DiskLow, ...]:
        rule = comms.registry.storage_rule
        free = rule.free_gib()
        member = DiskFloorLow if rule.below_floor(free) else cls
        return (member(rule=rule, free_gib=free),) if rule.cleanup_due(free) else ()

    def floor_warning(self) -> str:
        return ""

    def line(self) -> str:
        return (
            f"FREE {self.free_gib:.1f} GiB free on {self.rule.volume} (cleanup due under"
            f" {self.rule.cleanup_gib:g} GiB{self.floor_warning()}). who's cleaning up?"
            " -> put a cleanup owner on it now"
        )


@dataclass(frozen=True)
class DiskFloorLow(DiskLow):
    """The allocation-floor warning, selected by DiskLow rather than a second rule scan."""

    def floor_warning(self) -> str:
        return f"; under the {self.rule.floor_gib:g} GiB floor nothing allocates"
