"""Lifecycle declarations supply membership; domain owners enforce operations."""

import pytest

from agent_comms.assignment_states import AssignmentState
from agent_comms.attempt_states import AttemptState
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.execution_states import ExecutionState
from agent_comms.goal_generation import GenerationState
from agent_comms.goal_states import GoalState
from agent_comms.lifecycle_capability import (
    Active, Complete, Deferred, Executing, Failed, LifecycleCapability, Pending,
    Successful, Terminal, Unresolved,
)
from agent_comms.obligation_states import PendingResponse, ResponseState
from agent_comms.pi_vocabulary import PiStopReason
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.turn_lease import TurnDeclaration
from test_coordination_store import ready, started


@pytest.mark.parametrize("family", [
    AssignmentState, AttemptState, ExecutionState, ResponseState, GenerationState,
    GoalState, PiStopReason, RecoveryCondition, TurnDeclaration,
])
@pytest.mark.parametrize("capability", [
    Terminal, Failed, Pending, Active, Executing, Unresolved, Deferred, Complete, Successful,
])
def test_requirement_and_projection_follow_the_declared_hierarchy(family, capability):
    work = object()
    for member in family.members_with(LifecycleCapability):
        refusal = ValueError("membership required")
        if capability in member.__mro__:
            member.require(capability, refusal)
            assert member.items_with(capability, work) == (work,)
        else:
            with pytest.raises(ValueError) as caught:
                member.require(capability, refusal)
            assert caught.value is refusal
            assert member.items_with(capability, work) == ()


@pytest.mark.parametrize("family", [
    AssignmentState, AttemptState, ExecutionState, ResponseState, GenerationState, GoalState,
])
def test_terminal_successors_cannot_resume(family):
    for member in family.members_with(Terminal):
        # CompletedGoal alone declares its legitimate idempotent self-transition.
        assert all(successor is member for successor in member.successors())


def test_terminal_attempts_refuse_nonatomic_advance_at_the_store(tmp_path):
    with ready(tmp_path / "coordination.sqlite3") as store:
        _, fence = started(store)
        before = store.get("exec")
        for member in AttemptState.members_with(Terminal):
            with pytest.raises(IdentityConflict, match="atomic settlement"):
                store.advance(fence, member, expected_pointer_revision=1)
        assert store.get("exec") == before


def test_deferred_responses_resume_atomically_in_the_store(tmp_path):
    for member in ResponseState.members_with(Deferred):
        with ready(tmp_path / f"{member.declared_name}.sqlite3") as store:
            original, = store.get("exec").obligations
            with store.session.writing() as db:
                ResponseObligation.update(
                    db, where="execution_id=? AND exact_target=?",
                    parameters=(original.execution_id, original.exact_target),
                    lifecycle=member(), revision=original.revision + 1,
                )
                deferred = ResponseObligation.one(
                    db, execution_id=original.execution_id, exact_target=original.exact_target
                )
                deferred.lifecycle.resume_response(store.session, deferred, original.execution_id)
            resumed, = store.get("exec").obligations
            assert resumed.lifecycle == PendingResponse()
            assert resumed.revision == deferred.revision + 1
