"""Declared lifecycle members execute the behavior supplied by their capabilities."""

import pytest

from agent_comms.assignment_states import AssignmentState
from agent_comms.attempt_states import AttemptState
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.declared_family import DeclaredFamily
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.execution_states import ExecutionState
from agent_comms.goal_generation import GenerationState
from agent_comms.goal_attempt_identity import FailureNotObserved
from agent_comms.goal_states import GoalState
from agent_comms.lifecycle_capability import (
    Active, Complete, Deferred, Executing, Failed, Pending, Successful, Terminal, Unresolved,
)
from agent_comms.obligation_states import PendingResponse, ResponseState
from agent_comms.pi_vocabulary import PiStopReason
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.turn_lease import TurnDeclaration
from test_coordination_store import ready


def declared_members(capability):
    pending, seen = [DeclaredFamily], set()
    while pending:
        family = pending.pop()
        if family in seen:
            continue
        seen.add(family)
        pending.extend(family.__subclasses__())
        if family._family_root is family:
            yield from family.members_with(capability)


@pytest.mark.parametrize("capability", [
    Terminal, Failed, Pending, Active, Executing, Unresolved, Deferred, Complete, Successful,
])
def test_every_declared_capability_member_projects_its_work(capability):
    members = tuple(declared_members(capability))
    assert members
    work = object()
    for member in members:
        assert member.items_with(capability, work) == (work,)


@pytest.mark.parametrize("family", [
    AssignmentState, AttemptState, ExecutionState, ResponseState, GenerationState, GoalState,
])
def test_terminal_states_require_atomic_settlement(family):
    for member in family.members_with(Terminal):
        # CompletedGoal alone declares an idempotent self-transition; it cannot resume.
        assert all(successor is member for successor in member.successors())
        with pytest.raises(IdentityConflict, match="atomic settlement"):
            member.require_nonatomic_advance(IdentityConflict)


@pytest.mark.parametrize("family", [PiStopReason, ResponseState])
def test_successful_states_contribute_and_refuse_failure_evidence(family):
    for member in family.members_with(Successful):
        assert member.success_count() == 1
        member.require_successful_completion(ValueError("not a successful completion"))
        with pytest.raises(ValueError, match="not failure evidence"):
            member.reject_success_as_failure(ValueError("not failure evidence"))


def test_failed_attempts_support_interrupted_executions():
    for member in AttemptState.members_with(Failed):
        state = member.load(None, True, True)
        state.require_failure(FailureNotObserved())
        assert state.accepts_interrupted_execution()


def test_pending_responses_refuse_folding():
    for member in ResponseState.members_with(Pending):
        state = member()
        assert state.items_with(Pending, state) == (state,)
        with pytest.raises(ValueError, match="open work"):
            state.require_no_pending(ValueError("open work"))


def test_active_goals_supply_their_active_work():
    for member in GoalState.members_with(Active):
        state = member()
        state.require_active()
        assert state.items_with(Active, state) == (state,)


def test_executing_turns_satisfy_execution_requirement():
    for member in TurnDeclaration.members_with(Executing):
        turn = member("turn", 1)
        turn.require_executing()
        assert turn.items_with(Executing, turn) == (turn,)


def test_unresolved_incidents_remain_available_for_review():
    for member in RecoveryCondition.members_with(Unresolved):
        state = member()
        state.require_review()
        assert state.items_with(Unresolved, state) == (state,)


def test_complete_goals_have_no_unsettled_work():
    for member in GoalState.members_with(Complete):
        state = member()
        state.assert_complete()
        assert state.items_with(Complete, state) == (state,)


def test_deferred_responses_resume_atomically_in_the_store(tmp_path):
    for member in ResponseState.members_with(Deferred):
        with ready(tmp_path / f"{member.declared_name}.sqlite3") as store:
            original, = store.snapshots.get("exec").obligations
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
            resumed, = store.snapshots.get("exec").obligations
            assert resumed.lifecycle == PendingResponse()
            assert resumed.revision == deferred.revision + 1
