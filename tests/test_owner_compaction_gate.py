"""Blocker-1 contract tests: canonical owner compaction attestation recheck.

Provider-free and runtime-dormant. The attestation must fail closed on any
owner epoch, active turn, goal, or liveness change between capture and
commit-time recheck. The native session fence is echoed, never judged here.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_comms.declarations import (
    Goal,
    RelationViolationError,
    Thread,
    ThreadRegistry,
)
from agent_comms.owner_compaction_gate import OwnerCompactionAttestation

FENCE = {
    "session_file": "/tmp/pr48-fence/session.jsonl",
    "session_leaf": "a" * 32,
    "session_revision": "2043:17:1000:2000",
}


def make_registry(tmp_path) -> tuple[ThreadRegistry, Thread, int]:
    registry = ThreadRegistry(tmp_path / "registry.json")
    owner = Thread(
        name="owner",
        tags=frozenset(),
        worktree=str(tmp_path),
        pid=__import__("os").getpid(),
        goal=Goal("original task", "goal-1", revision=4),
    )
    registry.register(owner)
    live, epoch = registry.live_owner_with_epoch("owner")
    return registry, live, epoch


def claim(registry: ThreadRegistry, owner: Thread, epoch: int, turn: str) -> tuple[Thread, int]:
    claimed, claimed_epoch = registry.claim_live_turn_with_epoch(owner, turn, expected_epoch=epoch)
    return claimed, claimed_epoch


def attest(
    registry: ThreadRegistry,
    owner: Thread,
    epoch: int,
    turn: str = "turn-1",
    goal: Goal | None = None,
    correction_revision: int = 7,
) -> OwnerCompactionAttestation:
    goal = goal or owner.goal
    return registry.attest_owner_compaction(
        owner,
        epoch,
        turn,
        expected_goal_id=goal.id,
        expected_goal_revision=goal.revision,
        correction_revision=correction_revision,
        **FENCE,
    )


def test_positive_attestation_echoes_owner_and_fence(tmp_path) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    claimed, claimed_epoch = claim(registry, owner, epoch, "turn-1")
    receipt = attest(registry, claimed, claimed_epoch)
    assert receipt.thread == "owner"
    assert receipt.owner_epoch == claimed_epoch
    assert receipt.turn_id == "turn-1"
    assert receipt.goal_id == "goal-1"
    assert receipt.goal_revision == 4
    assert receipt.correction_revision == 7
    assert receipt.registry_revision is not None
    assert receipt.session_file == FENCE["session_file"]
    assert receipt.session_leaf == FENCE["session_leaf"]
    assert receipt.session_revision == FENCE["session_revision"]


def test_recheck_binds_same_store_revision(tmp_path) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    claimed, claimed_epoch = claim(registry, owner, epoch, "turn-1")
    first = attest(registry, claimed, claimed_epoch)
    second = attest(registry, claimed, claimed_epoch)
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
            replace(own, goal=replace(own.goal, status="paused", revision=own.goal.revision + 1))
        ),
    ],
    ids=["stopped", "goal-replaced", "goal-progress", "goal-paused"],
)
def test_attestation_fails_closed_after_owner_or_goal_change(tmp_path, mutate) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    claimed, claimed_epoch = claim(registry, owner, epoch, "turn-1")
    mutate(registry, claimed)
    with pytest.raises((RelationViolationError, ValueError)):
        attest(registry, claimed, claimed_epoch)


def test_stale_epoch_after_second_turn_claim_fails(tmp_path) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    claimed, claimed_epoch = claim(registry, owner, epoch, "turn-1")
    registry.unregister("owner")
    revived = replace(claimed, active_turn=None)
    registry.register(revived)
    _, second_epoch = claim(registry, revived, registry.snapshot().owner_epochs["owner"], "turn-2")
    assert second_epoch != claimed_epoch
    with pytest.raises(RelationViolationError):
        attest(registry, claimed, claimed_epoch)  # pre-restart epoch is stale
    # Even the current epoch fails: the turn id no longer matches the claim.
    with pytest.raises(RelationViolationError):
        attest(registry, revived, second_epoch, turn="turn-1")


def test_unclaimed_turn_cannot_attest(tmp_path) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    with pytest.raises(RelationViolationError):
        attest(registry, owner, epoch)


def test_stale_goal_expectation_fails(tmp_path) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    claimed, claimed_epoch = claim(registry, owner, epoch, "turn-1")
    stale_goal = claimed.goal
    registry.register(replace(claimed, goal=replace(claimed.goal, revision=99)))
    with pytest.raises(RelationViolationError):
        attest(registry, claimed, claimed_epoch, goal=stale_goal)


def test_non_owner_process_cannot_attest(tmp_path, monkeypatch) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    claimed, claimed_epoch = claim(registry, owner, epoch, "turn-1")
    monkeypatch.setattr("agent_comms.declarations.os.getpid", lambda: owner.pid + 1)
    with pytest.raises(RelationViolationError):
        attest(registry, claimed, claimed_epoch)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_epoch": 0},
        {"turn_id": ""},
        {"turn_id": "t" * 129},
        {"expected_goal_id": ""},
        {"expected_goal_revision": -1},
        {"correction_revision": -1},
        {"session_file": ""},
        {"session_leaf": ""},
        {"session_revision": ""},
    ],
)
def test_malformed_expectations_rejected(tmp_path, kwargs) -> None:
    registry, owner, epoch = make_registry(tmp_path)
    claimed, claimed_epoch = claim(registry, owner, epoch, "turn-1")
    request = {
        "expected_epoch": claimed_epoch,
        "turn_id": "turn-1",
        "expected_goal_id": "goal-1",
        "expected_goal_revision": 4,
        "correction_revision": 7,
        **FENCE,
    }
    request.update(kwargs)
    with pytest.raises(ValueError):
        registry.attest_owner_compaction(claimed, **request)
