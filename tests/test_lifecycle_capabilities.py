"""Lifecycle members execute the behavior supplied by their capabilities."""

import pytest

from agent_comms.assignment_states import AssignmentState
from agent_comms.attempt_states import AttemptState
from agent_comms.execution_states import ExecutionState
from agent_comms.obligation_states import ResponseState
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.lifecycle_capability import Successful, Terminal
from agent_comms.pi_vocabulary import PiStopReason


@pytest.mark.parametrize("family", [AssignmentState, AttemptState, ExecutionState, ResponseState])
def test_terminal_states_require_atomic_settlement(family):
    for member in family.members_with(Terminal):
        assert member.successors() == ()
        with pytest.raises(IdentityConflict, match="atomic settlement"):
            member.require_nonatomic_advance()


@pytest.mark.parametrize("family", [PiStopReason, ResponseState])
def test_successful_states_contribute_and_refuse_failure_evidence(family):
    for member in family.members_with(Successful):
        assert member.success_count() == 1
        member.require_successful_completion(ValueError("not a successful completion"))
        with pytest.raises(ValueError, match="not failure evidence"):
            member.reject_success_as_failure(ValueError("not failure evidence"))
