"""Goal publication callbacks remain inspectable by runtime consumers."""

from collections.abc import Callable
from inspect import isfunction
from types import NoneType
from typing import get_args, get_origin, get_type_hints

import pytest

from agent_comms.context_segments.wake_values import WakeWorkContext
from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.goal_attempts import LaunchPermit
from agent_comms.goal_attempt_phase import GoalAttemptPhase
from agent_comms.goal_generation import GenerationState
from agent_comms.goal_ledger import FailedTurnEvidence, FailedTurnObservation, Generation
from agent_comms.goal_failure_observation import NoFailedTurnEvidence
from agent_comms.goal_turn_permit import GoalTurnPermit
from agent_comms.goal_actions import GoalAction, SetGoalAction, StandbyGoalAction
from agent_comms.goal_mutation import GoalSnapshotRead
from agent_comms.goal_wait_target import GoalWaitSenderRead, GoalWaitTarget
from agent_comms.goal_records import GoalRecords
from agent_comms.goal_states import GoalState
from agent_comms.goals import GoalDeclaration, GoalMentionBinding, GoalOrder, GoalUnavailable
from agent_comms.goal_work import GoalFailureOwner, GoalOwner, GoalWorkSource
from agent_comms.registry.threads import Thread
from agent_comms.relationships import RelationshipSource, ThreadRelationships
from agent_comms.thread_presentation import ThreadOwnerBinding
from agent_comms.thread_identity import TurnFence
from agent_comms.turn_failure import NativePiTerminalFailure, RetryableProviderFailure, TurnFailure


@pytest.mark.parametrize(
    ("callback", "result"),
    (
        (GoalUnavailable.heading, str),
        (Thread.wake_context, WakeWorkContext),
        (Thread.contains_worktree, bool),
        (Thread.owner_binding, ThreadOwnerBinding),
        *((member.owner_binding, ThreadOwnerBinding) for member in Thread.__subclasses__()),
        (GoalDeclaration.settle, NoneType),
        *((member.settle, NoneType) for member in GoalDeclaration.__subclasses__()),
        (GoalDeclaration.clear_wait, bool),
        *((member.clear_wait, bool) for member in GoalDeclaration.__subclasses__()),
        (GoalDeclaration.stopped_permit, GoalTurnPermit),
        *((member.stopped_permit, GoalTurnPermit) for member in GoalDeclaration.__subclasses__()),
    ),
)
def test_goal_publication_callback_annotations_resolve(
    callback: Callable[..., object], result: type,
) -> None:
    assert get_type_hints(callback)["return"] is result


@pytest.mark.parametrize("callback", (GoalAction.prepare, StandbyGoalAction.prepare))
def test_goal_preparation_uses_its_record_owner(callback: Callable[..., object]) -> None:
    assert get_type_hints(callback)["goals"] is GoalRecords


@pytest.mark.parametrize("declaration", (GoalDeclaration, *GoalDeclaration.__subclasses__()))
def test_goal_scope_callback_keeps_its_scope_type(declaration: type[GoalDeclaration]) -> None:
    hints = get_type_hints(declaration.task_scope)
    assert hints["scope"] is hints["return"]
    assert get_args(hints["scoped"])[1] is hints["return"]


@pytest.mark.parametrize("order", (GoalOrder, *GoalOrder.__subclasses__()))
def test_goal_order_reads_its_assignment_source(order: type[GoalOrder]) -> None:
    assert get_type_hints(order.todos)["goals"] is GoalWorkSource


@pytest.mark.parametrize("declaration", (GoalDeclaration, *GoalDeclaration.__subclasses__()))
def test_goal_instructions_read_their_assignment_source(declaration: type[GoalDeclaration]) -> None:
    assert get_type_hints(declaration.instructions_for)["goals"] is GoalWorkSource


@pytest.mark.parametrize("declaration", (GoalDeclaration, *GoalDeclaration.__subclasses__()))
def test_goal_context_callback_keeps_its_context_type(declaration: type[GoalDeclaration]) -> None:
    hints = get_type_hints(declaration.add_context)
    assert get_args(hints["prepend_goal"])[1] is hints["context"]
    assert hints["return"] is hints["context"]


@pytest.mark.parametrize("declaration", (GoalDeclaration, *GoalDeclaration.__subclasses__()))
def test_goal_assignment_checks_use_their_owner_read_contract(declaration: type[GoalDeclaration]) -> None:
    assert get_type_hints(declaration.require_work)["owner"] is GoalOwner


@pytest.mark.parametrize("binding", (GoalMentionBinding, *GoalMentionBinding.__subclasses__()))
def test_goal_mention_projection_constructor_contracts_resolve(binding: type[GoalMentionBinding]) -> None:
    hints = get_type_hints(binding.project)
    assert "make_contact" in hints and "make_diagnostic" in hints and "return" in hints


@pytest.mark.parametrize("order", (*ThreadSort.members_with(ThreadSort), *ChannelSort.members_with(ChannelSort)))
def test_display_choice_runtime_contracts_resolve(order: type[ThreadSort] | type[ChannelSort]) -> None:
    assert get_type_hints(order.store_channel_order)
    assert get_type_hints(order.key)


def test_relationship_projection_contract_resolves() -> None:
    assert get_type_hints(ThreadRelationships.__init__)["views"] is RelationshipSource
    assert get_type_hints(RelationshipSource.thread_views)["return"]


@pytest.mark.parametrize("state", GoalState.members_with(GoalState))
def test_goal_state_boundary_contracts_resolve(state: type[GoalState]) -> None:
    assert get_type_hints(state.execution)["return"]
    hints = get_type_hints(state.accept_order)
    assert hints["goal"] is hints["return"]


@pytest.mark.parametrize("callback", (FailedTurnObservation.from_terminal, LaunchPermit.failure_observation))
def test_failed_attempt_reads_original_identity_contracts(callback: Callable[..., object]) -> None:
    hints = get_type_hints(callback)
    assert hints["owner"] is GoalFailureOwner
    assert hints["lease"] is TurnFence


def test_attempt_context_callback_preserves_its_result_type() -> None:
    hints = get_type_hints(LaunchPermit.add_context)
    assert get_args(hints["context"])[-1] is hints["return"]


@pytest.mark.parametrize("reading", (NoFailedTurnEvidence, FailedTurnEvidence))
def test_failure_projection_contract_preserves_its_result(reading: type) -> None:
    hints = get_type_hints(reading.execution)
    assert get_args(hints["source"])[0] is hints["return"]


def test_generation_failure_read_names_its_stored_row() -> None:
    assert get_args(get_type_hints(Generation.failure_evidence)["return"]) == (FailedTurnEvidence,)


@pytest.mark.parametrize(
    "state",
    (GenerationState, *GenerationState.members_with(GenerationState),
     GoalAttemptPhase, *GoalAttemptPhase.members_with(GoalAttemptPhase),
     TurnFailure, *TurnFailure.members_with(TurnFailure), RetryableProviderFailure),
)
def test_lifecycle_method_annotations_resolve(state: type) -> None:
    for name, member in vars(state).items():
        match member:
            case classmethod(__func__=callback) | staticmethod(__func__=callback):
                pass
            case _:
                callback = member
        if isfunction(callback) and callback.__annotations__:
            assert get_type_hints(callback), f"{state.__name__}.{name}"


def test_native_terminal_failure_keeps_its_original_proof_type() -> None:
    hints = get_type_hints(NativePiTerminalFailure.__init__)
    assert hints["context"] is NativePiTerminalFailure.__type_params__[0]
    assert hints["failure"] is TurnFailure


@pytest.mark.parametrize("action", (GoalAction, SetGoalAction))
def test_goal_client_reads_only_the_original_goal(action: type[GoalAction]) -> None:
    hints = get_type_hints(action.submit_control)
    snapshot = get_args(get_args(hints["observe"])[-1])[0]
    assert get_origin(snapshot) is GoalSnapshotRead
    assert get_args(snapshot) == (GoalDeclaration,)


def test_dependency_reply_reads_its_original_sender_identity() -> None:
    assert get_type_hints(GoalWaitTarget.sent)["original"] is GoalWaitSenderRead
