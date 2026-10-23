"""Replay the privately copied October 23 fleet through Core's rule family."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Annotated

import pytest

from agent_comms.fleet_replay import FleetInputs, FleetRootCopy
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.comms import Comms
from agent_comms.active_route import LocalRoute
from agent_comms.phase_evidence import MainTree
from agent_comms.repo import Repo
from agent_comms.standing_rules import StandingRule
from agent_comms.todos import CheckoutReads, PresentCheckout, PullRequests, SharedWorktreeRefusal
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.work_branches import WorkBranches


@dataclass(frozen=True)
class ExpectedIncident:
    rule: StandingRule
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ExpectedAssignment:
    problem: str
    holder: ThreadIncarnation


@dataclass(frozen=True)
class ExpectedSharedWorktree:
    worktree: Annotated[Path, PathText]
    attempt: ExpectedAssignment
    others: tuple[ExpectedAssignment, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ExpectedFleet:
    incidents: tuple[ExpectedIncident, ...]
    shared_worktree: ExpectedSharedWorktree


@pytest.fixture
def recorded_fleet(monkeypatch):
    directory = Path.home() / ".cache/agent-scratch/fleet-replay-2026-10-23"
    copy = FieldCodec.decode(FleetRootCopy, FieldCodec.parse((directory / "copy.json").read_bytes()))
    inputs = FieldCodec.decode(FleetInputs, FieldCodec.parse((directory / "inputs.json").read_bytes()))
    expected = FieldCodec.decode(ExpectedFleet, FieldCodec.parse(
        (Path(__file__).parent / "fixtures/fleet-2026-10-23-incidents.json").read_bytes()))
    original_repo_git, original_checkout_git = Repo.git, PresentCheckout.git

    def repo_git(checkout, *arguments):
        return original_repo_git(checkout, *inputs.checkout(Path(checkout)).arguments(arguments))

    def checkout_git(checkout, *arguments):
        return original_checkout_git(checkout, *inputs.checkout(checkout.path).arguments(arguments))

    def branch_difference(reader, path):
        return inputs.branch_difference(path)

    def unpublished(reader, difference):
        return inputs.unpublished(reader.checkout)

    def pull_diff(reader, base, head):
        return inputs.comparison(reader.checkout, base, head)

    def listed(reader, repo, selection, todo):
        return inputs.listed(repo, selection, todo)

    def worktrees(reader):
        return {row.branch: row.paths for row in inputs.checkout(reader.checkout).worktrees}

    monkeypatch.setattr(time, "time", lambda: inputs.captured_at)
    monkeypatch.setattr(Repo, "git", staticmethod(repo_git))
    monkeypatch.setattr(PresentCheckout, "git", checkout_git)
    monkeypatch.setattr(CheckoutReads, "branch_difference", branch_difference)
    monkeypatch.setattr(WorkBranches, "unpushed", unpublished)
    monkeypatch.setattr(WorkBranches, "pull_request_diff", pull_diff)
    monkeypatch.setattr(WorkBranches, "uncommitted", lambda reader: inputs.checkout(reader.checkout).uncommitted)
    monkeypatch.setattr(WorkBranches, "local_work", property(
        lambda reader: inputs.checkout(reader.checkout).holds_work))
    monkeypatch.setattr(WorkBranches, "worktrees", worktrees)
    monkeypatch.setattr(PullRequests, "read", listed)
    monkeypatch.setattr(MainTree, "of", classmethod(lambda cls, repo: inputs.main_tree(repo)))
    return copy, expected


def test_replay_raises_each_incident_once(recorded_fleet):
    copy, expected = recorded_fleet
    first = copy.observations()
    actual = tuple(rule for commander in first for rule in commander.rules)
    expected_rules = tuple(incident.rule for incident in expected.incidents)
    assert expected_rules, "The dated fixture must contain independently checked real incidents"
    selected = {type(rule) for rule in expected_rules}
    assert Counter(rule for rule in actual if type(rule) in selected) == Counter(expected_rules)
    assert all(count == 1 for count in Counter(expected_rules).values())
    assert all(incident.evidence for incident in expected.incidents)

    conflict = expected.shared_worktree
    comms = Comms(LocalRoute(copy.root))
    commander, = first
    with pytest.raises(SharedWorktreeRefusal) as refused:
        with comms.goals.todos.writing() as database:
            comms.goals.todos.require_assignment_in(database, conflict.attempt.problem,
                                                  conflict.attempt.holder,
                                                  commander.thread.incarnation)
    assert refused.value.problem == conflict.attempt.problem
    assert refused.value.holder == conflict.attempt.holder
    assert refused.value.worktree == conflict.worktree
    assert ExpectedAssignment(refused.value.holding_problem,
                              refused.value.holding_holder) in conflict.others
    assert conflict.evidence

    second = copy.observations()
    assert tuple(commander.thread.incarnation for commander in first) == tuple(
        commander.thread.incarnation for commander in second)
    for before, after in zip(first, second, strict=True):
        assert StandingRule.heartbeat_changes(before.rules, after.rules) == ()
