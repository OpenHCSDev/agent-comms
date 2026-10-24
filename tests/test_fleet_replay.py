"""Replay a small fleet written through Core's own writers: each covered incident is raised once.

Git is real (a temporary repository, origin remote, unit homes and allocated order checkouts);
only forge answers come from the external gh fixture, through the unmodified reader.
"""

import os
import subprocess
import time
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from agent_comms.commander.publication_rules import EmptyPullRequest, UnpushedBranch
from agent_comms.commander.spinning import SpinningUnit
from agent_comms.commander_heartbeat import CommanderObservation
from agent_comms.comms import Comms
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.native_tool_call import ToolCallId
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Checkout
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import (
    BranchBehindMain,
    BranchGrowth,
    DeliveredHeadReview,
    HeldBranchRule,
    LostTools,
    SharedFileBranches,
    StandingRule,
)
from agent_comms.thread_identity import ReportedTools, ToolName
from agent_comms.thread_text import ForkedParent
from agent_comms.threads import Thread
from agent_comms.todos import (
    ClaimOrder,
    OrderClaim,
    OrderMessage,
    PendingProblemOrder,
    ProgressRead,
    PullRequestLink,
    SharedWorktreeRefusal,
    Todo,
    UnmergedListedPullRequest,
)
from agent_comms.transcript_events import ToolStartTranscript
from agent_comms.turn_item import NativeItemId, NativeTurnId
from agent_comms.work_branches import BranchDifference, SourceGrowth, UnpushedHead
from conftest import declare_test_bus
from fixture_git import IDENTITY
from github_fixture import GitHubFixture
from source_revision_fixture import SourceRepository

pytest_plugins = ("github_fixture",)


@dataclass
class Fleet:
    """One commander, one repository and the units its seeds add."""

    comms: Comms
    source: SourceRepository
    worktrees: Path
    old_main: str
    request: str
    github: GitHubFixture
    problems: dict[str, str] = field(default_factory=dict)
    commander: ClassVar[str] = "lead"

    @property
    def repository(self) -> RepositoryName:
        return self.source.github

    @property
    def main(self) -> str:
        return self.git(self.source.checkout, "rev-parse", "refs/remotes/origin/main")

    @staticmethod
    def git(checkout: Path, *arguments: str, at: float | None = None) -> str:
        """Real Git; a past ``at`` dates the commit and the HEAD reflog entry it writes."""
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        if at is not None:
            environment["GIT_AUTHOR_DATE"] = environment["GIT_COMMITTER_DATE"] = f"@{int(at)}"
        return subprocess.run(
            ("git", *IDENTITY, "-C", str(checkout), *arguments),
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    def observe(self) -> CommanderObservation:
        return CommanderObservation.read(
            self.comms, self.comms.registry.require(self.commander)
        )

    def thread(self, name: str) -> Thread:
        return self.comms.registry.require(name)

    def worktree(self, branch: str, *, base: str, at: float | None = None) -> Path:
        path = self.worktrees / branch
        self.git(
            self.source.checkout, "worktree", "add", "-q", "-b", branch, str(path), base, at=at
        )
        return path

    def commit(self, worktree: Path, name: str, text: str, *, at: float | None = None) -> str:
        (worktree / name).parent.mkdir(parents=True, exist_ok=True)
        (worktree / name).write_text(text)
        self.git(worktree, "add", name, at=at)
        self.git(worktree, "commit", "-q", "-m", f"Change {name}", at=at)
        return self.git(worktree, "rev-parse", "HEAD")

    def push(self, worktree: Path) -> None:
        self.git(worktree, "push", "-q", "-u", "origin", "HEAD")

    def unit(self, name: str, worktree: Path, *, at: float | None = None) -> Todo:
        """Register a home, publish its order and populate the allocated order checkout."""
        declare_test_bus(
            self.comms,
            threads=(Thread(name=name, tags=frozenset({"comms"}), worktree=str(worktree)),),
        )
        todo = self.order(name, worktree, at=time.time() if at is None else at)
        rule = self.comms.registry.storage_rule
        checkout = rule.order_checkout(rule.initial_roots(name), todo.id)
        branch = self.git(worktree, 'rev-parse', '--abbrev-ref', 'HEAD')
        self.git(worktree, 'switch', '--detach', at=at)
        self.git(worktree, 'worktree', 'add', '-q', str(checkout), branch, at=at)
        return todo

    def order(self, name: str, worktree: Path, *, at: float) -> Todo:
        problem = self.problems.setdefault(name, f"P{len(self.problems) + 1}")
        scope = SourceRepository.scope()
        self.comms.goals.update_goal(
            name,
            CreateProblemGoalAction(
                problem=problem,
                commander=self.commander,
                request=self.request,
                assigner=self.thread(self.commander).incarnation,
                repo=self.repository,
                text=f"Repair {name}",
                review_at=RecordedEventTime(time.time() + 3600),
                order=PendingProblemOrder(
                    id=f"order-{name}",
                    owner=scope.shape.parents[0],
                    scope=scope,
                    run=MechanicalOrderRun(),
                    check=OrderCheck(worktree, "printf 0", CountCheckResult(1)),
                    edits=("Repair the existing owner",),
                    banned=("No second store",),
                    report="Report the result",
                    at=RecordedEventTime(at),
                    first_call_deadline=RecordedEventTime(time.time() + 3600),
                ),
            ),
        )
        return self.comms.goals.todos.get(problem)

    def claim(self, todo: Todo, number: int, head: str, *, at: float) -> OrderClaim:
        """The unit delivers through the claim writer, linking its pull request to the order."""
        lead = self.thread(self.commander).incarnation
        unit = self.thread(todo.held().owner).incarnation
        original = self.comms.messaging.send(
            self.commander, unit.name, todo.order.instructions(todo)
        ).message
        todo = self.comms.goals.todos.apply(
            todo.id, OrderMessage(todo.revision, lead, original.message_id)
        )
        claimed = OrderClaim.for_order(
            todo,
            unit,
            number,
            head,
            "Delivered the repair",
            original.reference,
            store=self.comms.goals.todos,
        )
        claimed = replace(claimed, message=replace(claimed.message, timestamp=at))
        ClaimOrder(todo.revision, unit, todo.held().generation, claimed).record(
            self.comms.goals, todo.id
        )
        return claimed

    def pull_request(self, number: int, branch: str, head: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.github.put(self.repository, UnmergedListedPullRequest(
            number, branch, head, stamp, "main", reviewable_times=(stamp,),
        ))


class IncidentSeed(ABC):
    """One covered standing rule: the fleet it needs and the single rule the replay must raise."""

    seeds: ClassVar[dict[type[StandingRule], type[IncidentSeed]]] = {}
    rule: ClassVar[type[StandingRule]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        IncidentSeed.seeds[cls.rule] = cls

    @classmethod
    @abstractmethod
    def seed(cls, fleet: Fleet) -> StandingRule: ...


class BranchBehindMainSeed(IncidentSeed):
    rule = BranchBehindMain

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        worktree = fleet.worktree("behind", base=fleet.old_main)
        fleet.commit(worktree, "behind.py", "behind = 1\n")
        fleet.push(worktree)
        todo = fleet.unit("behind", worktree)
        return BranchBehindMain(
            todo.id,
            todo.held().owner_identity,
            fleet.repository,
            "behind",
            BranchBehindMain.limit + 1,
        )


class SharedFileBranchesSeed(IncidentSeed):
    rule = SharedFileBranches

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        held = []
        for branch in ("shared-left", "shared-right"):
            worktree = fleet.worktree(branch, base=fleet.main)
            fleet.commit(worktree, "shared.py", f"side = {branch!r}\n")
            fleet.push(worktree)
            held.append(fleet.unit(branch, worktree))
        left, right = held
        return SharedFileBranches(
            left.id,
            left.held().owner_identity,
            "shared-left",
            right.id,
            right.held().owner_identity,
            "shared-right",
            fleet.repository,
            "shared.py",
        )


class BranchGrowthSeed(IncidentSeed):
    rule = BranchGrowth

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        worktree = fleet.worktree("growing", base=fleet.main)
        fleet.commit(
            worktree, "src/agent_comms/grown.py",
            "".join(f"v{i} = {i}\n" for i in range(SourceGrowth.limit + 1)),
        )
        fleet.push(worktree)
        todo = fleet.unit("growing", worktree)
        return BranchGrowth(
            todo.id, todo.held().owner_identity, fleet.repository,
            "growing", SourceGrowth((("src/agent_comms/grown.py", SourceGrowth.limit + 1),)),
        )


class UnpushedBranchSeed(IncidentSeed):
    rule = UnpushedBranch

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        worktree = fleet.worktree("unpushed", base=fleet.main)
        head = fleet.commit(worktree, "unpushed.py", "unpushed = 1\n")
        todo = fleet.unit("unpushed", worktree)
        unit = fleet.thread("unpushed")
        return UnpushedBranch(
            todo.id,
            todo.order.checked().id,
            unit.incarnation,
            fleet.comms.registry.storage_rule.order_checkout(
                fleet.comms.registry.storage_rule.initial_roots(unit.name), todo.id),
            UnpushedHead(head, 1, ""),
        )


class EmptyPullRequestSeed(IncidentSeed):
    rule = EmptyPullRequest

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        worktree = fleet.worktree("empty", base=fleet.main)
        fleet.commit(worktree, "empty.py", "empty = 1\n")
        fleet.push(worktree)
        todo = fleet.unit("empty", worktree)
        # The PR's head is main itself; a fresh claim is not yet owed a head review.
        fleet.pull_request(11, "empty", fleet.main)
        fleet.claim(todo, 11, fleet.main, at=time.time())
        return EmptyPullRequest(
            todo.id,
            todo.order.checked().id,
            todo.held().owner_identity,
            PullRequestLink(fleet.repository, 11),
            fleet.main,
            fleet.main,
        )


class DeliveredHeadReviewSeed(IncidentSeed):
    rule = DeliveredHeadReview

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        worktree = fleet.worktree("delivered", base=fleet.main)
        head = fleet.commit(worktree, "delivered.py", "delivered = 1\n")
        fleet.push(worktree)
        todo = fleet.unit("delivered", worktree)
        fleet.pull_request(12, "delivered", head)
        claimed = fleet.claim(
            todo, 12, head, at=time.time() - DeliveredHeadReview.minutes * 60 - 60
        )
        return DeliveredHeadReview(
            todo.id,
            todo.held().owner_identity,
            todo.order.checked().id,
            fleet.repository,
            12,
            head,
            claimed.message.timestamp + DeliveredHeadReview.minutes * 60,
        )


class SpinningUnitSeed(IncidentSeed):
    rule = SpinningUnit

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        spinning_turn_id = NativeTurnId('spinning')
        moved = time.time() - (SpinningUnit.minutes + 10) * 60
        worktree = fleet.worktree("spinning", base=fleet.main, at=moved)
        head = fleet.commit(worktree, "spinning.py", "spinning = 1\n", at=moved)
        fleet.push(worktree)
        todo = fleet.unit("spinning", worktree, at=moved - 60)
        transcript = ExternalTranscript(fleet.comms.root, todo.held().owner_identity)
        for call in range(SpinningUnit.calls):
            transcript.finish(
                turn_id=spinning_turn_id,
                item_id=NativeItemId(f"call-{call}"),
                events=(
                    ToolStartTranscript(
                        position=0,
                        tool_call_id=ToolCallId(f"call-{call}"),
                        tool_name="bash",
                        timestamp=moved + 60 + call,
                    ),
                ),
            )
        return SpinningUnit(
            todo.id, todo.held().owner_identity, SpinningUnit.calls, SpinningUnit.minutes, head
        )


class LostToolsSeed(IncidentSeed):
    rule = LostTools

    @classmethod
    def seed(cls, fleet: Fleet) -> StandingRule:
        registry = fleet.comms.registry
        declare_test_bus(
            fleet.comms,
            threads=(
                Thread(
                    name="restarted",
                    tags=frozenset({"comms"}),
                    worktree=str(fleet.source.checkout),
                    parent=ForkedParent(fleet.commander),
                ),
            ),
        )
        registry.observe_offered_tools(
            registry.require("restarted").incarnation,
            ReportedTools(
                "1", frozenset({ToolName("comms_send"), ToolName("comms_order_deliver")})
            ),
        )
        registry.register(registry.require("restarted"), new_owner=True)
        registry.observe_offered_tools(
            registry.require("restarted").incarnation,
            ReportedTools("1", frozenset({ToolName("comms_send")})),
        )
        return LostTools(
            registry.require("restarted").incarnation,
            ("missing tools comms_order_deliver; runtime 1",),
        )


@pytest.fixture
def fleet(comms, tmp_path, configured_repository, installed_runtime, github_cli):
    source = SourceRepository.create(tmp_path / "source")
    declare_test_bus(
        comms,
        channels=("comms",),
        threads=(
            Thread(name=Fleet.commander, tags=frozenset({"comms"}), worktree=str(source.checkout)),
        ),
    )
    request = comms.goals.todos.add_request(
        comms.registry.require(Fleet.commander).incarnation, "Keep the fleet on its plans"
    ).id
    project = configured_repository(comms)
    github_cli.configure(source.github, *(repo.github for repo in project.repos))
    old_main = source.main_commit
    built = Fleet(comms, source, tmp_path / "worktrees", old_main, request, github_cli)
    # Main moves past the oldest branch's base by exactly one commit more than the limit.
    for count in range(BranchBehindMain.limit + 1):
        built.commit(source.checkout, "main.py", f"count = {count}\n")
    built.git(source.checkout, "push", "-q", "origin", "main")
    expected = tuple(
        IncidentSeed.seeds[member].seed(built)
        for member in StandingRule.members_with(StandingRule)
        if member in IncidentSeed.seeds
    )
    return built, expected


def test_every_held_branch_rule_has_a_seed():
    assert set(HeldBranchRule.members_with(HeldBranchRule)) <= set(IncidentSeed.seeds)


def test_branch_rules_read_the_order_checkout_instead_of_the_behind_home(fleet):
    built, _ = fleet
    home = built.worktree('old-home', base=built.old_main)
    todo = built.unit('order-worker', home)
    rule = built.comms.registry.storage_rule
    checkout = rule.order_checkout(rule.initial_roots('order-worker'), todo.id)
    behind_line = f'BRANCH BEHIND MAIN: {todo.id} '
    built.git(built.source.checkout, 'worktree', 'remove', str(checkout))
    assert not any(rule.line().startswith(behind_line) for rule in built.observe().rules)

    built.git(built.source.checkout, 'worktree', 'add', '-q', '-b', 'current-order',
              str(checkout), built.main)
    assert not any(rule.line().startswith(behind_line) for rule in built.observe().rules)
    growth = SourceGrowth.limit + 1
    built.commit(checkout, 'src/agent_comms/order_growth.py',
                 ''.join(f'order_value_{index} = {index}\n' for index in range(growth)))
    assert built.git(home, 'rev-parse', 'HEAD') == built.old_main
    assert BranchGrowth(
        todo.id, todo.held().owner_identity, built.repository, 'current-order',
        SourceGrowth((('src/agent_comms/order_growth.py', growth),)),
    ) in built.observe().rules


def test_held_branch_rules_survive_an_order_checkout_retired_mid_frame(fleet):
    """A unit retires its checkout before reporting; the heartbeat reading it must not crash."""
    built, expected = fleet
    storage = built.comms.registry.storage_rule
    allocated = {storage.order_checkout(storage.initial_roots(name), problem)
                 for name, problem in built.problems.items()}

    class RetiringRead(ProgressRead):
        def branch_difference(self, path: Path) -> BranchDifference:
            difference = super().branch_difference(path)
            if path in allocated:
                Checkout(path).retire()
            return difference

    observed = CommanderObservation.read(
        built.comms, built.thread(Fleet.commander), RetiringRead)
    held = set(HeldBranchRule.members_with(HeldBranchRule))
    assert not any(path.exists() for path in allocated)
    assert Counter(rule for rule in observed.rules if type(rule) in held) == Counter(
        rule for rule in expected if type(rule) in held)


def test_replay_raises_each_incident_once(fleet):
    built, expected = fleet
    first = built.observe()
    seeded = {type(rule) for rule in expected}
    assert seeded == set(IncidentSeed.seeds)
    assert Counter(rule for rule in first.rules if type(rule) in seeded) == Counter(expected)
    assert all(count == 1 for count in Counter(expected).values())

    # A second holder in an occupied worktree is refused at order time.
    behind = built.thread("behind")
    declare_test_bus(
        built.comms,
        threads=(
            Thread(name="squatter", tags=frozenset({"comms"}), worktree=str(behind.project_path)),
        ),
    )
    with pytest.raises(SharedWorktreeRefusal) as refused:
        built.order("squatter", behind.project_path, at=time.time())
    assert refused.value.problem == built.problems["squatter"]
    assert refused.value.holder == built.thread("squatter").incarnation
    assert refused.value.worktree == behind.project_path
    assert refused.value.holding_problem == built.problems["behind"]
    assert refused.value.holding_holder == behind.incarnation

    second = built.observe()
    assert {rule.observation_key() for rule in first.rules} == {
        rule.observation_key() for rule in second.rules
    }
