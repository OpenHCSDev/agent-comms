"""One line per unit from live activity, registered Git checkouts and exact-head reviews."""
from __future__ import annotations

import argparse
import time
from abc import abstractmethod
from threading import Event, Thread as WorkerThread
from typing import ClassVar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import ToolRefusedError
from agent_comms.store_files import StoreLockBusyError
from agent_comms.head_review import PullRequestHead
from agent_comms.recorded_time import EventTime
from agent_comms.repo import GitCommandError
from agent_comms.threads import Thread
from agent_comms.todos import PresentCheckout, PullRequests, Todo
from agent_comms.work_branches import WorkBranches, WorkBranchError

from .order import Commander


@dataclass(frozen=True)
class FleetCheckout(PresentCheckout):
    """The existing checkout reader, projected without walking commit history."""

    def line(self, now: float) -> str:
        try:
            tip, = self.commits("", ("HEAD",))
            dirty = len(self.branches.uncommitted())
            return f"{self.path.name}:{tip.sha[:9]} {max(0, round(now - tip.at))}s dirty={dirty}"
        except GitCommandError as error:
            return f"{self.path.name}:UNREADABLE {error.stderr.strip()}"


@dataclass(frozen=True)
class FleetUnit:
    thread: Thread
    state: str
    last_tool: EventTime
    orders: tuple[Todo, ...]
    checkouts: tuple[FleetCheckout, ...]
    pulls: tuple[PullRequestHead, ...]
    idle_holding: bool

    def line(self, commander: Commander, now: float) -> str:
        reviews = tuple(
            f"{request.repo.to_wire()}#{request.pr}@{request.head[:9]}"
            f" verdict={','.join(record.verdict.declared_name for record in commander.comms.goals.todos.head_reviews(request)) or '-'}"
            for request in self.pulls
        )
        return (
            f"{self.thread.name} {self.state} tool={self.last_tool.age_seconds(now)} "
            f"order={','.join(str(order.order.checked().id) for order in self.orders) or '-'} "
            f"git=[{'; '.join(checkout.line(now) for checkout in self.checkouts) or '-'}] "
            f"pr=[{'; '.join(reviews) or '-'}]"
            + (" IDLE-HOLDING" if self.idle_holding else "")
        )


@dataclass(frozen=True)
class Fleet:
    """A read-only join; every source remains owned by Core, Git or the forge."""

    units: tuple[FleetUnit, ...]
    now: float

    @classmethod
    def read(cls, commander: Commander) -> Fleet:
        comms = commander.comms
        snapshot = comms.registry.snapshot()
        threads = tuple(snapshot.active_threads(
            snapshot.require(name) for name in sorted(commander.units())))
        held = commander.held_orders()
        activities = comms.agents.all_activity(threads, snapshot=snapshot)
        rule = comms.registry.storage_rule
        pulls = PullRequests(held)
        # Read each repository's actual Git registry once, never glob the worktree directory.
        def repository(repo):
            registered = tuple((Path(path).resolve(), branch) for branch, paths in
                               WorkBranches(repo.checkout).worktrees().items() for path in paths)
            return repo.github, registered, pulls.open(repo.github)

        with ThreadPoolExecutor(max_workers=8) as workers:
            repositories = tuple(workers.map(repository, commander.repos))

            def unit(thread: Thread) -> FleetUnit:
                orders = tuple(todo for todo in held if todo.holder == thread.name)
                owned = tuple((repo, path, branch) for repo, paths, _ in repositories
                              for path, branch in paths
                              if rule.owns_present_checkout(thread, path))
                linked = {(link.repo, link.number) for todo in orders
                          for link in todo.order.checked().pull_requests}
                requests = tuple(PullRequestHead(repo, request.number, request.head)
                                 for repo, _, requests in repositories for request in requests
                                 if (repo, request.number) in linked or any(
                                     repo == owner_repo and request.branch == branch
                                     for owner_repo, _, branch in owned))
                activity = activities[thread.name]
                return FleetUnit(
                    thread, activity.state.value, thread.runtime.history_source(
                        comms.root, thread.incarnation).last_tool_call(), orders,
                    tuple(FleetCheckout(path) for _, path, _ in owned), requests,
                    bool(orders) and not activity.state.busy,
                )

            units = tuple(workers.map(unit, threads))
        return cls(tuple(sorted(units, key=lambda unit: (not unit.idle_holding, unit.thread.name))), time.time())

    def lines(self, commander: Commander) -> tuple[str, ...]:
        # Checkout reads are independent and bounded by the registered live fleet.
        with ThreadPoolExecutor(max_workers=8) as workers:
            return tuple(workers.map(lambda unit: unit.line(commander, self.now), self.units))


@dataclass(frozen=True)
class FleetEvent(DeclaredFamily, affix="Event"):
    """One observation in tool-watch's stream; its declaration owns its scan and fix."""

    text: str
    repeat_seconds: ClassVar[float] = 60
    fix: ClassVar[str]

    def line(self) -> str:
        return f"{self.declared_name}: {self.text} -> {self.fix}"

    @classmethod
    @abstractmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]: ...


class UnitFleetEvent(FleetEvent):
    repeat_seconds = 600
    fix = "agent-comms-commander order work PROBLEM"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return tuple(cls(line) for line in fleet.lines(commander))


class FleetReadFailedEvent(FleetEvent):
    fix = "agent-comms-commander fleet"

    @classmethod
    def observed(cls, commander: Commander, fleet: Fleet) -> tuple[FleetEvent, ...]:
        return ()


class FleetWatch(WorkerThread):
    """A bounded read worker; slow forge or store reads cannot stop native tool output."""

    def __init__(self, commander: Commander) -> None:
        super().__init__(name="fleet-watch", daemon=True)
        self.commander = commander
        self.stopping = Event()
        self.next_emit: dict[type[FleetEvent], float] = {}

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                fleet = Fleet.read(self.commander)
                for member in FleetEvent.members_with(FleetEvent):
                    if fleet.now >= self.next_emit.get(member, 0):
                        for event in member.observed(self.commander, fleet):
                            print(event.line(), flush=True)
                        self.next_emit[member] = fleet.now + member.repeat_seconds
            except (StoreLockBusyError, ToolRefusedError, WorkBranchError, GitCommandError) as error:
                print(FleetReadFailedEvent(str(error)).line(), flush=True)
            self.stopping.wait(60)


def main(argv: list[str], commander: Commander) -> None:
    argparse.ArgumentParser(prog="fleet", description=__doc__).parse_args(argv)
    fleet = Fleet.read(commander)
    print("\n".join(fleet.lines(commander)))
