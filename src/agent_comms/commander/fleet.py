"""Read the current fleet and stream its alarms beside native tool calls."""
from __future__ import annotations

import argparse
import time
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from threading import Event, Thread as WorkerThread
from typing import ClassVar

from agent_comms.commander.questions import UnansweredUnitAsks
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import ToolRefusedError
from agent_comms.head_review import PullRequestHead
from agent_comms.held_orders import HeldOrders
from agent_comms.installer.launch import InstallLogs
from agent_comms.messages import Message
from agent_comms.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.repo import GitCommandError, Repo
from agent_comms.store_files import StoreLockBusyError
from agent_comms.thread_status import CliInboxPresence
from agent_comms.threads import Thread
from agent_comms.tool_declaration import one_line
from agent_comms.todos import (
    DraftListedPullRequest, HeadReviewRecord, PresentCheckout, PullRequests, Todo,
    UnmergedListedPullRequest,
)
from agent_comms.work_branches import WorkBranches, WorkBranchError

from .order import Commander


@dataclass(frozen=True)
class FleetCheckout(PresentCheckout):
    """Project Git facts without walking unrelated branches or commit history."""

    def line(self, now: float) -> str:
        try:
            tip, = self.commits("", ("HEAD",))
            dirty = len(self.branches.uncommitted())
            return f"{self.path.name}:{tip.sha[:9]} {max(0, round(now - tip.at))}s dirty={dirty}"
        except GitCommandError as error:
            return f"{self.path.name}:UNREADABLE {error.stderr.strip()}"

    def last_push(self, request: UnmergedListedPullRequest) -> EventTime:
        reference = f"refs/remotes/origin/{request.branch}"
        refs = tuple(ref for ref in self.origin_refs if ref == reference)
        # No clock is better than pretending a commit's date is its push time.
        return next((RecordedEventTime(at) for head, at in
                     self.recorded_pushes(refs, "--max-count=1") if head == request.head),
                    UnrecordedEventTime())

    def empty(self, request: UnmergedListedPullRequest) -> bool:
        base = self.git("merge-base", f"refs/remotes/origin/{request.base}", request.head).strip()
        return not self.branches.changed(base, request.head)


@dataclass(frozen=True)
class FleetBranch:
    name: str
    checkout: FleetCheckout


@dataclass(frozen=True)
class FleetRequest:
    head: PullRequestHead
    listed: UnmergedListedPullRequest
    reviews: tuple[HeadReviewRecord, ...]

    def line(self) -> str:
        verdict = ','.join(record.verdict.state for record in self.reviews) or '-'
        return f"{self.head.repo.to_wire()}#{self.head.pr}@{self.head.head[:9]} verdict={verdict}"


@dataclass(frozen=True)
class FleetRepository:
    repo: Repo
    branches: tuple[FleetBranch, ...]
    requests: tuple[FleetRequest, ...]
    checkout: FleetCheckout

    @classmethod
    def read(cls, repo: Repo, pulls: PullRequests, commander: Commander) -> FleetRepository:
        return cls(repo, tuple(FleetBranch(branch, FleetCheckout(Path(path).resolve()))
                               for branch, paths in WorkBranches(repo.checkout).worktrees().items()
                               for path in paths), tuple(
            FleetRequest(head := PullRequestHead(repo.github, request.number, request.head), request,
                         commander.comms.goals.todos.head_reviews(head))
            for request in pulls.open(repo.github)), FleetCheckout(repo.checkout))


@dataclass(frozen=True)
class FleetUnit:
    thread: Thread
    state: str
    last_tool: EventTime
    orders: tuple[Todo, ...]
    checkouts: tuple[FleetCheckout, ...]
    requests: tuple[FleetRequest, ...]
    idle_holding: bool

    def line(self, now: float) -> str:
        return (
            f"{self.thread.name} {self.state} tool={self.last_tool.age_seconds(now)} "
            f"order={','.join(str(order.order.checked().id) for order in self.orders) or '-'} "
            f"git=[{'; '.join(checkout.line(now) for checkout in self.checkouts) or '-'}] "
            f"pr=[{'; '.join(request.line() for request in self.requests) or '-'}]"
            + (" IDLE-HOLDING" if self.idle_holding else "")
        )


@dataclass(frozen=True)
class Fleet:
    """One read's lazy sources; a failed forge read cannot hide bridge or install exits."""

    commander: Commander
    now: float

    @classmethod
    def read(cls, commander: Commander) -> Fleet:
        return cls(commander, time.time())

    @cached_property
    def snapshot(self) -> RegistrySnapshot:
        return self.commander.comms.registry.snapshot()

    @cached_property
    def scan(self) -> HeldOrders:
        return HeldOrders.read(self.commander.comms, self.commander.commander)

    @cached_property
    def pulls(self) -> PullRequests:
        return PullRequests()

    @cached_property
    def repositories(self) -> tuple[FleetRepository, ...]:
        pulls = self.pulls
        with ThreadPoolExecutor(max_workers=8) as workers:
            return tuple(workers.map(lambda repo: FleetRepository.read(
                repo, pulls, self.commander), self.commander.repos))

    @cached_property
    def questions(self) -> tuple[Message, ...]:
        questions = UnansweredUnitAsks.for_orders(
            self.commander.comms.bus.bus, self.snapshot, self.commander.commander,
            self.scan.holders, self.now - 300)
        return tuple(message for asked in questions.values() for message in asked)

    @cached_property
    def units(self) -> tuple[FleetUnit, ...]:
        commander = self.commander
        comms = commander.comms
        threads = tuple(self.snapshot.active_threads(
            self.snapshot.require(name) for name in sorted(commander.units())))
        held = self.scan.todos
        repositories = self.repositories
        activities = comms.agents.all_activity(threads, snapshot=self.snapshot)
        rule = comms.registry.storage_rule

        def unit(thread: Thread) -> FleetUnit:
            orders = tuple(todo for todo in held if todo.holder == thread.name)
            owned = tuple(branch for repository in repositories for branch in repository.branches
                          if rule.owns_checkout(thread, branch.checkout.path, repository.repo.github))
            linked = {(link.repo, link.number) for todo in orders
                      for link in todo.order.checked().pull_requests}
            requests = tuple(request for repository in repositories
                             for request in repository.requests
                             if (request.head.repo, request.head.pr) in linked or any(
                                 branch in owned and request.listed.branch == branch.name
                                 for branch in repository.branches))
            activity = activities[thread.name]
            return FleetUnit(
                thread, activity.state.value, thread.runtime.history_source(
                    comms.root, thread.incarnation).last_tool_call(), orders,
                tuple(branch.checkout for branch in owned), requests,
                bool(orders) and not activity.state.busy,
            )

        with ThreadPoolExecutor(max_workers=8) as workers:
            units = tuple(workers.map(unit, threads))
        return tuple(sorted(units, key=lambda unit: (not unit.idle_holding, unit.thread.name)))

    def lines(self) -> tuple[str, ...]:
        with ThreadPoolExecutor(max_workers=8) as workers:
            return tuple(workers.map(lambda unit: unit.line(self.now), self.units))


@dataclass(frozen=True)
class FleetEvent(DeclaredFamily, affix="Event"):
    """Each declared member owns its observation and the verb that fixes it."""

    text: str
    repeat_seconds: ClassVar[float] = 60
    fix: ClassVar[str]

    def line(self) -> str:
        return f"{self.declared_name}: {self.text} -> {self.fix}"

    @classmethod
    @abstractmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]: ...


class BridgeStoppedEvent(FleetEvent):
    fix = "agent-comms-claude-bridge connect"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        snapshot = fleet.snapshot
        thread = snapshot.require(commander.commander)
        return tuple(cls(f"{bridge.name} bridge process {bridge.process_identity} exited")
                     for bridge in snapshot.status(thread.name).items_with(CliInboxPresence, thread)
                     if not bridge.process_identity.alive())


class InstallFailedEvent(FleetEvent):
    fix = "agent-comms-commander order install"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return tuple(cls(f"{unit.unit}: {reason}") for unit in InstallLogs(commander.root).units()
                     for reason in unit.failure_reasons())


class UnansweredQuestionEvent(FleetEvent):
    fix = "agent-comms-commander order say UNIT FILE --answers MESSAGE_ID"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return tuple(cls(f"{message.sender} {message.message_id}, unanswered "
                         f"{round(fleet.now - message.timestamp)}s: {one_line(message.body, 400)}")
                     for message in fleet.questions)


class DraftEvent(FleetEvent, declared_name=None):
    seconds: ClassVar[float] = 1800

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        events: list[FleetEvent] = []
        for repository in fleet.repositories:
            for request in repository.requests:
                for draft in request.listed.as_draft():
                    try:
                        events.extend(cls.for_draft(repository, request, draft, fleet.now))
                    except GitCommandError as error:
                        events.append(FleetReadFailedEvent(
                            f"{request.head.repo.to_wire()}#{request.head.pr}: {error.stderr.strip()}"))
        return tuple(events)

    @classmethod
    @abstractmethod
    def for_draft(cls, repository: FleetRepository, request: FleetRequest,
                  draft: DraftListedPullRequest, now: float) -> tuple[FleetEvent, ...]: ...


class StaleDraftEvent(DraftEvent):
    fix = "git push; finish the proof and mark the PR ready"

    @classmethod
    def for_draft(cls, repository: FleetRepository, request: FleetRequest,
                  draft: DraftListedPullRequest, now: float) -> tuple[FleetEvent, ...]:
        pushed = repository.checkout.last_push(draft)
        return (cls(f"{request.line()} last push {pushed.age_seconds(now)} ago"),
                ) if pushed.before(now - cls.seconds) else ()


class EmptyDraftEvent(DraftEvent):
    fix = "commit and push the implementation; finish the proof and mark the PR ready"

    @classmethod
    def for_draft(cls, repository: FleetRepository, request: FleetRequest,
                  draft: DraftListedPullRequest, now: float) -> tuple[FleetEvent, ...]:
        return (cls(f"{request.line()} empty after {round(now - draft.reviewable_at)}s"),
                ) if now >= draft.reviewable_at + cls.seconds and repository.checkout.empty(draft) else ()


class ReadyHeadUnjudgedEvent(FleetEvent):
    fix = "agent-comms-commander judge reviewing --pr PR --head HEAD"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return tuple(cls(request.line()) for repository in fleet.repositories
                     for request in repository.requests if request.listed.review_due(fleet.now, 0)
                     and not any(record.verdict.decides() for record in request.reviews))


class RejectedAutoMergeEvent(FleetEvent):
    fix = "gh pr merge PR --disable-auto; fix the sent-back head"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return tuple(cls(request.line()) for repository in fleet.repositories
                     for request in repository.requests if request.listed.armed_times
                     and any(record.verdict.rejects_merge() for record in request.reviews))


class UnitFleetEvent(FleetEvent):
    repeat_seconds = 600
    fix = "agent-comms-commander order work PROBLEM"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return tuple(cls(line) for line in fleet.lines())


class FleetReadFailedEvent(FleetEvent):
    fix = "agent-comms-commander fleet"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return ()


class FleetWatch(WorkerThread):
    """Slow forge or store reads cannot stop the native tool stream."""

    def __init__(self, commander: Commander) -> None:
        super().__init__(name="fleet-watch", daemon=True)
        self.commander = commander
        self.stopping = Event()
        self.next_emit: dict[type[FleetEvent], float] = {}

    def run(self) -> None:
        while not self.stopping.is_set():
            fleet = Fleet.read(self.commander)
            for member in FleetEvent.members_with(FleetEvent):
                if fleet.now < self.next_emit.get(member, 0):
                    continue
                try:
                    for event in member.observed(self.commander, fleet):
                        print(event.line(), flush=True)
                except (StoreLockBusyError, ToolRefusedError, WorkBranchError,
                        GitCommandError, FileNotFoundError) as error:
                    print(FleetReadFailedEvent(f"{member.declared_name}: {error}").line(), flush=True)
                self.next_emit[member] = fleet.now + member.repeat_seconds
            self.stopping.wait(60)


def main(argv: list[str], commander: Commander) -> None:
    argparse.ArgumentParser(prog="fleet", description=__doc__).parse_args(argv)
    print("\n".join(Fleet.read(commander).lines()))
