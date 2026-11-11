"""Required nominal provider pairs and unchanged concrete source records."""

from inspect import isabstract
from typing import get_type_hints

import pytest

from agent_comms.execution.native_failure_reads import (
    NativeOperationFailureRead,
    NativeResponseErrorRead,
    NativeResponseFailureRead,
)
from agent_comms.execution.provider_records import ReportedText
from agent_comms.execution.runtime_info_source import RuntimeInfoWrites
from agent_comms.execution.turn_failure_reads import (
    GoalFailurePermit,
    GoalFailureReservation,
    ModelFailureDiagnostic,
    ReplayPreparation,
    TerminalInputsRead,
    TerminalOutputRead,
    TerminalPromptRead,
    TerminalSessionRead,
)
from agent_comms.execution.turn_progress_reads import ProgressGoalAccount, ProgressTurn
from agent_comms.messaging.activity_clocks import ActivityClocks
from agent_comms.messaging.display_sources import (
    ActivitySource,
    ChannelOrderSource,
    ChannelSortView,
    SentClocks,
    SentHistorySource,
    SortedChannel,
    StoredThreadOrder,
)
from agent_comms.planning.goal_failure_owner import GoalFailureOwner
from agent_comms.planning.goal_ledger import AttemptRecord, Generation, Reservation
from agent_comms.planning.goal_work import (
    GoalAssignment,
    GoalAssignments,
    GoalObservation,
    GoalOwner,
)
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.execution.backend import NativeSession
from agent_comms.messaging.channel_management import ChannelManagement
from agent_comms.coordination.tables.executions import ReplayAssessments
from agent_comms.execution.goal_turn_permit import NoLaunchPermit
from agent_comms.execution.owned_turn import OwnedTurn, TurnGoalAccount
from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.execution.turn_inputs import InputForwarding
from agent_comms.execution.turn_output import TurnOutput
from agent_comms.history_views import HistoryViews
from agent_comms.messaging.channels import Channel
from agent_comms.messaging.published_bus import BusResources
from agent_comms.execution.pi_events import NativeOperationError
from agent_comms.execution.pi_events import Response
from agent_comms.execution.pi_wire import PiDiagnostic
from agent_comms.execution.pi_wire import PiOmitted
from agent_comms.planning.goal_attempt_phase import GoalAttemptTransition
from agent_comms.planning.goal_attempts import LaunchPermit
from agent_comms.planning.goal_failure_observation import GenerationFailureSource
from agent_comms.planning.goals import GoalDeclaration
from agent_comms.planning.orders import ProblemOrder, Todo, TodoStore
from agent_comms.presentation import ChannelView
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.execution.prompt_state import PromptState
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_reads import ThreadCheckoutSource, ThreadOwnerSource, ThreadOwnerStatus
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread
from agent_comms.registry.relationships import RelationshipSource


@pytest.mark.parametrize("provider, contract", (
    (ActivityClocks, SentClocks), (BusResources, ActivitySource),
    (HistoryViews, SentHistorySource), (Channel, StoredThreadOrder),
    (ChannelManagement, ChannelOrderSource), (Channel, SortedChannel),
    (ChannelView, ChannelSortView), (HistoryViews, RelationshipSource),
    (GoalDeclaration, GoalObservation), (Thread, GoalOwner), (Thread, GoalFailureOwner),
    (Todo, GoalAssignment), (TodoStore, GoalAssignments),
    (AttemptRecord, GoalAttemptTransition), (Generation, GenerationFailureSource),
    (OwnedTurn, ProgressTurn), (TurnGoalAccount, ProgressGoalAccount),
    (PiDiagnostic, ModelFailureDiagnostic), (PromptState, TerminalPromptRead),
    (InputForwarding, TerminalInputsRead), (TurnOutput, TerminalOutputRead),
    (NativeSession, TerminalSessionRead), (ReplayAssessments, ReplayPreparation), (Reservation, GoalFailureReservation),
    (LaunchPermit, GoalFailurePermit), (ReportedText, NativeResponseErrorRead),
    (PiOmitted, NativeResponseErrorRead), (Response, NativeResponseFailureRead),
    (NativeOperationError, NativeOperationFailureRead), (AgentActivity, RuntimeInfoWrites),
    (StorageRule, ThreadCheckoutSource), (ThreadStatus, ThreadOwnerStatus),
    (RegistrySnapshot, ThreadOwnerSource),
))
def test_original_providers_declare_the_required_read_capabilities(provider, contract):
    assert contract in provider.__mro__
    assert contract.__slots__ == ()


@pytest.mark.parametrize("provider", (
    TurnGoalAccount, OwnedTurn, PromptState, InputForwarding, TurnOutput,
    ReplayAssessments, Reservation, LaunchPermit, ReportedText, PiOmitted,
    Response, NativeOperationError, AgentActivity, StorageRule, RegistrySnapshot,
    ActivityClocks, HistoryViews, ChannelView, Todo, TodoStore, AttemptRecord, Generation,
))
def test_original_providers_remain_concrete(provider):
    assert not isabstract(provider)


def test_original_native_wire_and_order_fields_remain_concrete():
    assert get_type_hints(Response)["error"] == ReportedText | PiOmitted
    assert get_type_hints(NativeOperationError)["error"] is str
    assert get_type_hints(LaunchPermit)["reservation"] is Reservation
    assert get_type_hints(ChannelView)["channel"] is Channel
    assert get_type_hints(Thread)["goal"] is GoalDeclaration
    assert get_type_hints(Todo)["order"] is ProblemOrder
    assert GoalFailurePermit not in NoLaunchPermit.__mro__
    assert NoLaunchPermit().claims == ()


def test_replay_stays_a_class_and_native_text_retains_omission():
    assert ReplayAssessments.revoke.__self__ is ReplayAssessments
    assert ReplayAssessments.authorize_if_unobserved.__self__ is ReplayAssessments
    assert get_type_hints(TurnFailure.prepare_execution_retry)["replay"] == type[ReplayPreparation]
    assert PiOmitted().text_or("fallback") == "fallback"
    assert ReportedText("").text_or("fallback") == "fallback"
    assert ReportedText("").reported
    assert ReportedText("").value == ""
    assert not PiOmitted().reported
