"""Blocker-1 contract tests: canonical owner compaction attestation recheck.

Provider-free and runtime-dormant. The attestation must fail closed on any
owner epoch, active turn, goal, or liveness change between capture and
commit-time recheck. The native session fence is echoed, never judged here.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.goal_states import PausedGoal
from agent_comms.goals import Goal
from agent_comms.owner_compaction_gate import OwnerCompactionAttestation
from agent_comms.registration import Registration
from agent_comms.threads import Thread

FENCE = {
    "session_file": "/tmp/pr48-fence/session.jsonl",
    "session_leaf": "a" * 32,
    "session_revision": "2043:17:1000:2000",
}


def make_registry(tmp_path) -> tuple[Registration, Thread, int]:
    registry = Registration(tmp_path / "registry.json")
    owner = Thread(
        name="owner",
        tags=frozenset(),
        worktree=str(tmp_path),
        process_identity=ProcessIdentity.capture(__import__("os").getpid()),
        goal=Goal("original task", "goal-1", revision=4),
    )
    registry.register(owner)
    live, owner_generation = registry.live_owner_with_generation("owner")
    return registry, live, owner_generation


def lease(
    registry: Registration, owner: Thread, owner_generation: int, turn: str
) -> tuple[Thread, int]:
    leased, leased_generation = registry.lease_live_turn_with_generation(
        owner, turn, expected_owner_generation=owner_generation
    )
    return leased, leased_generation


def attest(
    registry: Registration,
    owner: Thread,
    owner_generation: int,
    turn: str = "turn-1",
    goal: Goal | None = None,
) -> OwnerCompactionAttestation:
    goal = goal or owner.goal
    return registry.attest_owner_compaction(
        owner,
        owner_generation,
        turn,
        expected_goal_id=goal.id,
        expected_goal_revision=goal.revision,
        **FENCE,
    )


def test_positive_attestation_echoes_owner_and_fence(tmp_path) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    leased, leased_generation = lease(registry, owner, owner_generation, "turn-1")
    receipt = attest(registry, leased, leased_generation)
    assert receipt.thread == "owner"
    assert receipt.owner_generation == leased_generation
    assert receipt.turn_id == "turn-1"
    assert receipt.goal_id == "goal-1"
    assert receipt.goal_revision == 4
    assert receipt.registry_revision is not None
    assert receipt.session_file == FENCE["session_file"]
    assert receipt.session_leaf == FENCE["session_leaf"]
    assert receipt.session_revision == FENCE["session_revision"]


def test_recheck_binds_same_store_revision(tmp_path) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    leased, leased_generation = lease(registry, owner, owner_generation, "turn-1")
    first = attest(registry, leased, leased_generation)
    second = attest(registry, leased, leased_generation)
    assert first.registry_revision == second.registry_revision


@pytest.mark.parametrize(
    "mutate",
    [
        lambda reg, own: reg.unregister("owner"),
        lambda reg, own: reg.register(replace(own, goal=Goal("new task", "goal-2", revision=5))),
        lambda reg, own: reg.register(
            replace(
                own,
                goal=replace(own.goal, progress="advanced", revision=own.goal.revision + 1),
            )
        ),
        lambda reg, own: reg.register(
            replace(own, goal=replace(own.goal, state=PausedGoal(), revision=own.goal.revision + 1))
        ),
    ],
    ids=["stopped", "goal-replaced", "goal-progress", "goal-paused"],
)
def test_attestation_fails_closed_after_owner_or_goal_change(tmp_path, mutate) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    leased, leased_generation = lease(registry, owner, owner_generation, "turn-1")
    mutate(registry, leased)
    with pytest.raises((RelationViolationError, ValueError)):
        attest(registry, leased, leased_generation)


def test_stale_generation_after_second_turn_lease_fails(tmp_path) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    leased, leased_generation = lease(registry, owner, owner_generation, "turn-1")
    registry.unregister("owner")
    revived = replace(leased, active_turn=None)
    registry.register(revived)
    _, second_generation = lease(
        registry, revived, registry.snapshot().owner_generations["owner"], "turn-2"
    )
    assert second_generation != leased_generation
    with pytest.raises(RelationViolationError):
        attest(registry, leased, leased_generation)  # pre-restart epoch is stale
    # Even the current epoch fails: the turn id no longer matches the claim.
    with pytest.raises(RelationViolationError):
        attest(registry, revived, second_generation, turn="turn-1")


def test_unclaimed_turn_cannot_attest(tmp_path) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    with pytest.raises(RelationViolationError):
        attest(registry, owner, owner_generation)


def test_stale_goal_expectation_fails(tmp_path) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    leased, leased_generation = lease(registry, owner, owner_generation, "turn-1")
    stale_goal = leased.goal
    registry.register(replace(leased, goal=replace(leased.goal, revision=99)))
    with pytest.raises(RelationViolationError):
        attest(registry, leased, leased_generation, goal=stale_goal)


def test_non_owner_process_cannot_attest(tmp_path, monkeypatch) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    leased, leased_generation = lease(registry, owner, owner_generation, "turn-1")
    other_pid = os.getppid()
    monkeypatch.setattr("agent_comms.store_files.os.getpid", lambda: other_pid)
    with pytest.raises(RelationViolationError):
        attest(registry, leased, leased_generation)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_owner_generation": 0},
        {"expected_owner_generation": True},
        {"expected_owner_generation": 1.0},
        {"turn_id": 7},
        {"expected_goal_revision": False},
        {"expected_goal_revision": 4.0},
        {"expected_goal_id": None},
        {"expected_goal_revision": None},
        {"session_file": 3},
        {"session_leaf": []},
        {"session_revision": True},
        {"turn_id": ""},
        {"turn_id": "t" * 129},
        {"expected_goal_id": ""},
        {"expected_goal_revision": -1},
        {"session_file": ""},
        {"session_leaf": ""},
        {"session_revision": ""},
    ],
)
def test_malformed_expectations_rejected(tmp_path, kwargs) -> None:
    registry, owner, owner_generation = make_registry(tmp_path)
    leased, leased_generation = lease(registry, owner, owner_generation, "turn-1")
    request = {
        "expected_owner_generation": leased_generation,
        "turn_id": "turn-1",
        "expected_goal_id": "goal-1",
        "expected_goal_revision": 4,
        **FENCE,
    }
    request.update(kwargs)
    with pytest.raises(ValueError):
        registry.attest_owner_compaction(leased, **request)
