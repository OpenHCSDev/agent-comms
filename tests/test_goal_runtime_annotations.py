"""Goal publication callbacks remain inspectable by runtime consumers."""

import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from inspect import isabstract, isfunction
from pathlib import Path
from types import NoneType
from typing import get_args, get_origin, get_type_hints

import pytest

from agent_comms import agent_events
from agent_comms.cli_bridges.shared import CliBridge, LineMonitorBridge
from agent_comms.execution.acp import AcpSelectedWriteAuthority
from agent_comms.execution.acp import CommsAgent
from agent_comms.execution.acp import SdkSessionConnection
from agent_comms.acp_extension import AgentCommsUpdate, LiveQueueScope, PromptRequest, QueueScope
from agent_comms.agent_event_updates import AcpEventConsumer, AcpToolAction
from agent_comms.agent_runtime import PublishesTranscript, ReadsHistory
from agent_comms.channel_input_batch import InputBatch
from agent_comms.cli_bridges.history import History
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOption, ConfigOptions
from agent_comms.context_segments.wake_values import WakeWorkContext
from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.execution.context_assembly import ContextAssembly
from agent_comms.execution.context_owner import ContextOwner
from agent_comms.execution.context_provenance_source import ContextProvenanceSource
from agent_comms.execution.delivers_input import DeliversInput
from agent_comms.execution.goal_scheduler import GoalScheduler
from agent_comms.execution.goal_turn_permit import GoalTurnPermit
from agent_comms.execution.input_drain import InputDrain
from agent_comms.execution.native_entries import ImportedSessionWriter
from agent_comms.execution.session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
from agent_comms.execution.turn_context import (
    CoordinationSegment,
    PeerState,
    Provenance,
    TurnContext,
)
from agent_comms.execution.turn_failure import (
    NativePiTerminalFailure,
    RetryableProviderFailure,
    TurnFailure,
)
from agent_comms.execution.turn_lease import TurnDeclaration
from agent_comms.execution.turn_origin import TurnOrigin
from agent_comms.execution.turn_progress import TurnProgress
from agent_comms.execution.turn_runner import TurnRunner
from agent_comms.following_inputs import FollowingInputs, OwnerFollowingInputs
from agent_comms.cli_bridges.harness_delivery_states import Outcome
from agent_comms.installer.harness_install import HarnessSession
from agent_comms.importing import ImportBuffer, ImportSnapshot
from agent_comms.input_attempt import InputAttempt, StoredInput
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.execution.input_key import InputKey
from agent_comms.execution.input_origin import InputProvenance
from agent_comms.messaging.messages import ClaimNoticeKind, Message, MessageInput
from agent_comms.messaging.response_message_source import ResponseMessageSource
from agent_comms.messaging.retained_task_facts import ExactTaskFact
from agent_comms.messaging.sending import Messaging
from agent_comms.nominal.field_codec import NullMember, PresentMember, ThreadLookup
from agent_comms.planning.goal_actions import GoalAction, SetGoalAction, StandbyGoalAction
from agent_comms.planning.goal_attempt_phase import GoalAttemptPhase
from agent_comms.planning.goal_attempts import LaunchPermit
from agent_comms.planning.goal_control import GoalControlSource
from agent_comms.planning.goal_failure_observation import NoFailedTurnEvidence
from agent_comms.planning.goal_failure_owner import GoalFailureOwner
from agent_comms.planning.goal_generation import GenerationState
from agent_comms.planning.goal_ledger import FailedTurnEvidence, FailedTurnObservation, Generation
from agent_comms.planning.goal_mutation import GoalTurnProjection
from agent_comms.planning.goal_records import GoalRecords
from agent_comms.planning.goal_states import GoalState
from agent_comms.planning.goal_wait_target import GoalWaitTarget
from agent_comms.planning.goal_waits import GoalWait, GoalWaits, NoGoalWait, RecordedGoalWait
from agent_comms.planning.goal_work import GoalOwner, GoalWorkSource
from agent_comms.planning.goals import (
    GoalDeclaration,
    GoalMentionBinding,
    GoalOrder,
    GoalUnavailable,
)
from agent_comms.private_send_claim import PrivateSendClaim
from agent_comms.registry.peer_relation import PeerRelation
from agent_comms.registry.registry_document import (
    RegistryDocument,
    RegistryPresence,
    RegistrySnapshot,
)
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry
from agent_comms.registry.thread_creation import ThreadCreationSource
from agent_comms.registry.thread_identity import TurnFence
from agent_comms.registry.thread_identity_source import ThreadIdentitySource
from agent_comms.registry.thread_presentation import ThreadOwnerBinding
from agent_comms.registry.threads import (
    Thread,
    ThreadProvenance,
)
from agent_comms.registry.relationships import RelationshipSource
from agent_comms.registry.relationships import ThreadRelationships
from agent_comms.messaging.response_policy import ResponsePolicy
from agent_comms.registry.route_client import ActivityClient
from agent_comms.messaging.routing import DeliveryScope
from agent_comms.messaging.routing import NoTurnOrigin
from agent_comms.messaging.routing import Routing
from agent_comms.messaging.routing import ScheduledTurn
from agent_comms.messaging.routing import TurnRouting
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate
from agent_comms.cli_bridges.runtime_updates import SessionFacts
from agent_comms.execution.session_config_options import SessionConfigOptions
from agent_comms.messaging.task_sources import ScopedTaskDeclaration
from agent_comms.execution.tracked_turn import TrackedTurnSession
from agent_comms.transcript.transcript_events import TranscriptEvent
from agent_comms.execution.transcript_routes import NoSentInputText
from agent_comms.execution.transcript_routes import SentInputText
from agent_comms.transport.tools.goals import CommsGoalHistoryTool, CommsGoalTool, GoalChangeTool


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
    (AcpEventConsumer, AcpToolAction, AgentCommsUpdate, LiveQueueScope, PromptRequest, QueueScope,
     GoalScheduler, ConfigOption, ConfigOptions, SessionConfigOptions,
     InputDrain, FollowingInputs, OwnerFollowingInputs, TrackedTurnSession, PrivateSendClaim,
     SessionLifecycle, AttachedSessionLifecycle, TurnRunner, AcpSelectedWriteAuthority,
     CommsAgent, SdkSessionConnection,
     GoalWait, NoGoalWait, RecordedGoalWait, GoalWaits,
     GoalChangeTool, CommsGoalTool, CommsGoalHistoryTool,
     *GoalChangeTool.members_with(GoalChangeTool),
     agent_events.InputStarted, *agent_events.InputStarted.__subclasses__(),
     agent_events.InputRefused, *agent_events.InputRefused.__subclasses__(),
     agent_events.Done, agent_events.CompactionEvent, agent_events.CompactionEnd,
     agent_events.TextEvent, agent_events.Notice, agent_events.RuntimeInfoObservation,
     agent_events.NativeMetadataObservation, agent_events.AgentEventConsumer,
     TurnProgress, ActivityClient,
     (task_scope := get_type_hints(Thread.task_scope.fget)["return"]),
     *task_scope.members_with(task_scope),
     (scope_selection := get_type_hints(Messaging.pin_user_constraint)["scope"]),
     *scope_selection.members_with(scope_selection),
     *ConfigOption.members_with(ConfigOption),
     GenerationState, *GenerationState.members_with(GenerationState),
     GoalAttemptPhase, *GoalAttemptPhase.members_with(GoalAttemptPhase),
     TurnFailure, *TurnFailure.members_with(TurnFailure), RetryableProviderFailure),
)
def test_declared_method_annotations_resolve(state: type) -> None:
    for name, member in vars(state).items():
        match member:
            case classmethod(__func__=callback) | staticmethod(__func__=callback):
                pass
            case _:
                callback = member
        if isfunction(callback) and callback.__annotations__:
            assert get_type_hints(callback), f"{state.__name__}.{name}"


def test_configuration_change_contract_keeps_its_launcher_type() -> None:
    source = get_origin(get_type_hints(ConfigOption.change)["owner"])
    assert get_type_hints(source.configure_thread)["configuration"] is source.__type_params__[0]


def test_session_configuration_source_keeps_its_proxy_type() -> None:
    source = get_origin(get_type_hints(SessionConfigOptions.__init__)["sessions"])
    assert get_args(get_type_hints(source)["proxies"])[1] is source.__type_params__[0]


def test_following_turn_source_keeps_its_sender_type() -> None:
    source = get_origin(get_type_hints(FollowingInputs.open)["session"])
    assert get_type_hints(source.forward_inbox)["sender"] is source.__type_params__[0]


def test_native_terminal_failure_keeps_its_original_proof_type() -> None:
    hints = get_type_hints(NativePiTerminalFailure.__init__)
    assert hints["context"] is NativePiTerminalFailure.__type_params__[0]
    assert hints["failure"] is TurnFailure


@pytest.mark.parametrize("action", (GoalAction, SetGoalAction))
def test_goal_client_uses_its_captured_control_source(action: type[GoalAction]) -> None:
    source = get_type_hints(action.submit_control)["source"]
    assert get_origin(source) is GoalControlSource
    assert get_args(source)[1] == GoalAction[GoalDeclaration]


@pytest.mark.parametrize(
    "declaration", (TurnDeclaration, *TurnDeclaration.members_with(TurnDeclaration)),
)
def test_turn_execution_projection_contract_resolves(declaration: type[TurnDeclaration]) -> None:
    hints = get_type_hints(declaration.execution)
    assert get_origin(hints["source"]) is GoalTurnProjection
    assert get_args(hints["source"])[0] is hints["return"]


def test_event_recording_keeps_the_captured_client_type() -> None:
    client = agent_events.AgentEventConsumer.__type_params__[0]
    assert get_type_hints(agent_events.AgentEventConsumer.__init__)["comms"] is client
    source = get_origin(client.__bound__)
    assert source is not None and issubclass(ActivityClient, source)
    assert get_type_hints(ActivityClient)["agents"] is get_type_hints(ActivityClient.activity_store)["return"]
    assert get_args(client.__bound__) == (agent_events.RuntimeInfoObservation,)
    assert any(
        get_origin(base) is agent_events.AgentEventConsumer and get_args(base) == (Comms,)
        for base in TurnProgress.__orig_bases__
    )


def test_event_notice_constructor_keeps_its_result_type() -> None:
    hints = get_type_hints(agent_events.Notice.acp_updates)
    assert get_args(hints["notice_update"])[-1] is get_args(hints["return"])[0]


def test_task_scope_uses_the_original_nominal_owner() -> None:
    scope = get_type_hints(Thread.task_scope.fget)["return"]
    owner = get_type_hints(scope.require_current)["owner"]
    source = get_origin(owner)
    assert source is not None and issubclass(Thread, source)
    assert get_args(owner) == (scope,)
    assert get_type_hints(source.task_scope.fget)["return"] is source.__type_params__[0]


def test_dependency_owner_annotations_resolve() -> None:
    assert get_type_hints(GoalRecords)["registry"]
    assert get_type_hints(GoalRecords._release_reviewed_wait)["edit"]
    assert get_type_hints(GoalRecords.consume_reply_wait)["owner"]


def test_dependency_reply_reads_its_original_sender_identity() -> None:

    assert get_type_hints(GoalWaitTarget.sent)["sender_lookup"] is ThreadLookup


@pytest.mark.parametrize("first_module", (
    'agent_comms.planning.goal_presentation',
    "agent_comms.planning.goal_execution",
    "agent_comms.planning.goal_events",
    'agent_comms.planning.goal_management',
    "agent_comms.agent_event_updates",
    'agent_comms.execution.goal_scheduler',
    SessionConfigOptions.__module__,
    "agent_comms.execution.session_reads",
    "agent_comms.acp_extension",
    "agent_comms.execution.queue_publication",
    "agent_comms.execution.input_drain",
    "agent_comms.execution.following_input_contracts",
    "agent_comms.following_inputs",
    TrackedTurnSession.__module__,
    "agent_comms.private_send_claim",
    "agent_comms.execution.session_tasks",
    "agent_comms.execution.session_owner_reads",
    "agent_comms.execution.session_lifecycle",
    'agent_comms.execution.turn_runner',
    CommsAgent.__module__,
    "agent_comms.planning.goal_waits",
    "agent_comms.transport.tools.goals",
    "agent_comms.agent_events",
    "agent_comms.execution.input_event_source",
    "agent_comms.execution.runtime_info_source",
    "agent_comms.execution.turn_progress_reads",
    'agent_comms.execution.turn_progress',
    ActivityClient.__module__,
    "agent_comms.planning.task_scope",
    "agent_comms.registry.task_scope_owner",
    ScopedTaskDeclaration.__module__,
    "agent_comms.execution.agent_event",
    "agent_comms.registry.registry_names",
    "agent_comms.registry.task_owner",
    "agent_comms.registry.task_registry",
    "agent_comms.registry.thread_identity_source",
    ExactTaskFact.__module__,
    'agent_comms.messaging.sending',
))
def test_goal_publication_contracts_resolve_cold(first_module: str) -> None:
    completed = subprocess.run(
        (sys.executable, str(Path(__file__).with_name("goal_publication_probe.py")), first_module),
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_task_namespace_contracts_are_inherited_by_original_providers() -> None:
    assert issubclass(ThreadProvenance, ThreadIdentitySource)
    assert issubclass(Thread, TaskOwner)
    assert issubclass(RegistryDocument, TaskRegistry)
    assert issubclass(RegistrySnapshot, TaskRegistry)
    for declaration in ScopedTaskDeclaration.members_with(ScopedTaskDeclaration):
        hints = get_type_hints(declaration.applies)
        assert hints["owner"] is TaskOwner
        assert get_origin(hints["registry"]) is TaskRegistry
        assert get_args(hints["registry"]) == (TaskOwner,)


def test_live_namespace_member_selection_retains_its_kind() -> None:
    method = RegistryPresence.require_member
    assert get_type_hints(method)["return"] is method.__type_params__[0]


def test_task_publication_validation_and_original_lease_contracts_resolve() -> None:
    assert get_type_hints(TaskOwner.require_publication_turn)["claim_generation"] == Callable[[], int]
    fence = get_type_hints(TaskOwner.require_turn_lease)["return"]
    assert issubclass(get_type_hints(Thread.require_turn_lease)["return"], fence)


def test_context_provenance_comes_from_the_original_stored_input() -> None:
    assert issubclass(StoredInput, ContextProvenanceSource)
    hints = get_type_hints(TurnContext.for_owner)
    assert hints["peer_owners"] == tuple[ContextOwner, ...]
    source = get_args(hints["inputs"])[0]
    assert get_origin(source) is ContextProvenanceSource
    assert get_args(source) == (Provenance,)
    assert get_type_hints(StoredInput.context_provenance)["return"] is InputProvenance
    assert get_type_hints(PeerState.from_owner)["owner"] is ContextOwner
    assert get_type_hints(CoordinationSegment.capture)["peer_owners"] == tuple[ContextOwner, ...]


def test_context_assembly_is_inherited_by_the_original_comms_owner() -> None:
    assert issubclass(Comms, ContextAssembly)
    assert Comms.context_for is ContextAssembly.context_for
    assert Comms.context_with_awareness is ContextAssembly.context_with_awareness
    hints = get_type_hints(Comms.context_for)
    assert hints["owner"] is ContextOwner
    assert hints["return"] is TurnContext
    awareness = get_type_hints(Comms.context_with_awareness)
    assert awareness["context"] is TurnContext
    assert awareness["owner"] is ContextOwner
    assert awareness["relations"] == tuple[PeerRelation, ...]
    assert awareness["return"] is TurnContext


def test_original_message_and_identity_provide_key_facts() -> None:
    assert issubclass(MessageInput, ResponseMessageSource)
    assert Message.bus_key is MessageInput.bus_key
    assert issubclass(ThreadIdentitySource, ThreadCreationSource)
    assert issubclass(Thread, ThreadCreationSource)
    source = get_type_hints(ResponseMessageSource)
    message = get_type_hints(MessageInput)
    assert source["seq"] is message["seq"]
    assert source["mentions"] == message["mentions"]
    assert get_type_hints(ThreadIdentitySource)["created_at"] is get_type_hints(ThreadProvenance)["created_at"]


def test_key_callbacks_read_the_original_identity_role() -> None:
    for callback in (MessageInput.bus_key, Message.bus_keys, NoTurnOrigin.bus_keys,
                     TurnOrigin.bus_keys):
        assert get_type_hints(callback)["owner"] is ThreadCreationSource
    assert get_type_hints(ClaimNoticeKind.claimed)["owner"] is ThreadIdentitySource
    assert get_type_hints(MessageInput.bus_key)["return"] is InputKey
    assert get_type_hints(Message.bus_keys)["return"] == tuple[InputKey, ...]


def test_original_import_factory_callbacks_use_actual_source_capabilities() -> None:
    source = get_type_hints(ImportSnapshot.read)["history"]
    assert issubclass(History, source)
    assert issubclass(source, ReadsHistory)
    assert issubclass(source, HarnessSession)
    assert get_type_hints(source.history)["return"] == AsyncIterator[RuntimeUpdate]
    original_value = get_type_hints(ImportedSessionWriter.preparing)["snapshot"]
    assert issubclass(ImportSnapshot, original_value)
    assert get_type_hints(ImportBuffer.describe)["facts"] is SessionFacts
    assert get_args(get_type_hints(ImportBuffer.add)["events"]) == (TranscriptEvent,)
    assert get_type_hints(PublishesTranscript.transcript)["return"] == AsyncIterator[RuntimeUpdate]
    delivery = get_type_hints(DeliversInput.deliver)
    assert delivery["message"] is DeliversInput.__type_params__[0]
    assert delivery["return"] == type[Outcome]
    bindings = tuple(get_args(base) for base in CliBridge.__orig_bases__
                     if get_origin(base) is DeliversInput)
    assert bindings == ((Message,),)
    assert issubclass(CliBridge, DeliversInput)
    concrete = get_type_hints(LineMonitorBridge.deliver)
    assert concrete["message"] is Message
    assert concrete["return"] == type[Outcome]


def test_origin_message_result_contract_preserves_original_providers() -> None:
    result = get_type_hints(TurnOrigin.messages)["return"]
    assert get_args(result)[0] is TurnOrigin.__type_params__[0]
    origin = get_type_hints(ScheduledTurn)["origin"]
    assert get_origin(origin) is TurnOrigin
    assert get_args(origin) == (Message,)
    assert issubclass(Message, TurnOrigin)
    assert issubclass(NoTurnOrigin, TurnOrigin)
    assert get_type_hints(NoTurnOrigin.messages)["return"] == tuple[Message, ...]


def test_routing_capture_reads_original_tagged_namespace() -> None:
    source = get_type_hints(DeliveryScope.capture)["snapshot"]
    assert get_origin(source) is RegistryNames
    owner, = get_args(source)
    assert issubclass(ThreadProvenance, owner)
    assert issubclass(Thread, owner)
    assert get_type_hints(owner)["tags"] == get_type_hints(ThreadProvenance)["tags"]
    assert get_type_hints(owner)["name"] is get_type_hints(ThreadProvenance)["name"]


def test_routing_presence_contract_keeps_original_sent_value_identity() -> None:
    assert issubclass(SentInputText, PresentMember)
    assert issubclass(NoSentInputText, NullMember)
    for routing in (Routing, TurnRouting):
        assert get_type_hints(routing.validate_input)["sent_text"] == PresentMember | NullMember


def test_original_input_capture_and_store_callbacks_resolve() -> None:
    capture = get_type_hints(InputBatch.capture)
    originals = capture["document"]
    assert originals in InputDocument.__bases__
    assert not isabstract(InputDocument)
    assert get_type_hints(originals.originals) == get_type_hints(InputDocument.originals)
    assert capture["owner"] is ThreadCreationSource
    assert get_args(capture["origins"]) == (Message, Ellipsis)
    for callback in (InputDocument.started_for_native, InputDocument.answered_by,
                     InputDispositions.context_committed, InputDispositions.reserve_turn,
                     InputDispositions.harness_attempts):
        get_type_hints(callback)


def test_input_observation_callbacks_use_original_evidence_contracts() -> None:
    for state in (InputAttempt, *InputAttempt.members_with(InputAttempt)):
        for callback in (state.context_committed, state.started_for_native, state.delivered_in):
            hints = get_type_hints(callback)
            if "lease" in hints:
                assert hints["lease"] is TurnFence
                assert get_origin(hints["snapshot"]) is RegistryNames
                assert get_args(hints["snapshot"]) == (ThreadIdentitySource,)


def test_declared_policy_keys_keep_their_original_message_and_identity_sources() -> None:
    for policy in ResponsePolicy.members_with(ResponsePolicy):
        hints = get_type_hints(policy.disposition_key)
        assert hints["message"] is ResponseMessageSource
        assert hints["owner"] is ThreadCreationSource
        assert hints["return"] is InputKey
