"""Original providers own the admitted goal read contracts and their concrete results."""

from inspect import isabstract
from typing import get_args, get_origin, get_type_hints

from agent_comms.execution.context_owner import ContextOwner
from agent_comms.execution.goal_turn_permit import GoalTurnPermit
from agent_comms.execution.turn_context import TurnContext
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_attempt_identity import GoalAttemptIdentity
from agent_comms.planning.goal_ledger import Generation, LedgerAttempt, Reservation
from agent_comms.planning.goal_mutation import (
    GoalContextSource,
    GoalGenerationRead,
    GoalStoppedAttempt,
)
from agent_comms.planning.goal_work import GoalWorkSource


def test_original_generation_declares_its_read_contract_without_changing_fields():
    assert GoalGenerationRead in Generation.__bases__
    assert not isabstract(Generation)
    assert get_type_hints(Generation)["goal_id"] is GoalId
    assert get_type_hints(Generation)["number"] is int
    assert get_type_hints(Generation.attempt_identity.fget)["return"] is GoalAttemptIdentity


def test_original_ledger_family_binds_the_concrete_reservation():
    assert GoalStoppedAttempt in LedgerAttempt.__bases__
    alias, = (base for base in LedgerAttempt.__orig_bases__
              if get_origin(base) is GoalStoppedAttempt)
    assert get_args(alias) == (Reservation,)
    assert get_type_hints(LedgerAttempt.stopped_permit)["return"] is GoalTurnPermit
    for member in LedgerAttempt.members_with(LedgerAttempt):
        assert issubclass(member, GoalStoppedAttempt)
        assert not isabstract(member)


def test_original_turn_context_declares_its_owner_and_goal_inputs():
    assert GoalContextSource in TurnContext.__bases__
    assert not isabstract(TurnContext)
    alias, = (base for base in TurnContext.__orig_bases__
              if get_origin(base) is GoalContextSource)
    assert get_args(alias)[:2] == (ContextOwner, GoalWorkSource)
    hints = get_type_hints(TurnContext.with_goal)
    assert hints["owner"] is ContextOwner
    assert hints["goals"] is GoalWorkSource
    assert hints["return"] is TurnContext
