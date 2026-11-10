"""Resolve goal publication contracts after one requested cold import."""

import importlib
import sys
from typing import get_type_hints

importlib.import_module(sys.argv[1])

from agent_comms.planning.goals import GoalUnavailable
from agent_comms.registry.threads import Thread

from agent_comms import agent_events
from agent_comms.context_segments.wake_values import WakeGoalContext
from agent_comms.acp_extension import AgentCommsUpdate, LiveQueueScope, PromptRequest, QueueScope
from agent_comms.execution.input_drain import DrainedSession, InputDrain, OwnerSession
from agent_comms.execution.session_lifecycle import SessionLifecycle
from agent_comms.execution.acp import SdkSessionConnection
from agent_comms.execution.session_owner_reads import OwnedSessionLifetime, SessionTaskOwner
from agent_comms.execution.session_reads import SessionClient
from agent_comms.execution.turn_runner import TurnRunner
from agent_comms.execution.following_input_contracts import (
    FollowingInputRetirement,
    FollowingPromptBoundary,
    FollowingTurnSource,
)
from agent_comms.following_inputs import OwnerFollowingInputs
from agent_comms.execution.native_execution import NativeExecution
from agent_comms.private_send_claim import PrivateSendClaim
from agent_comms.execution.tracked_turn import TrackedTurnSession
from agent_comms.execution.queue_publication import QueuePublication
from agent_comms.planning.goal_waits import GoalWait
from agent_comms.planning.goal_records import GoalRecords
from agent_comms.transport.tools.goals import CommsGoalTool
from agent_comms.planning.goal_management import Goals
from agent_comms.planning.goal_presentation import GoalSnapshot
from agent_comms.planning.goal_presentation import RunnableExecution
from agent_comms.nominal.field_codec import FieldCodec

assert get_type_hints(GoalUnavailable.heading)["execution"]
assert get_type_hints(Thread.wake_context)["goals"]
assert get_type_hints(RunnableExecution.apply_toggle)["change"]
assert get_type_hints(agent_events.InputStarted.captured)["inputs"]
assert get_type_hints(agent_events.AgentEventConsumer.__init__)["comms"]
assert get_type_hints(agent_events.Notice.acp_updates)["return"]
assert get_type_hints(GoalWait.visit_group)["agents"]
assert get_type_hints(GoalRecords)["registry"]
assert get_type_hints(CommsGoalTool.apply)["comms"]
assert issubclass(InputDrain, QueuePublication)
assert issubclass(DrainedSession, SessionTaskOwner)
assert issubclass(OwnerSession, OwnedSessionLifetime)
assert issubclass(SdkSessionConnection, SessionClient)
assert get_type_hints(SessionLifecycle.retire_owner)["state"] is OwnedSessionLifetime
assert get_type_hints(TurnRunner.close_sessions)["owners"] == tuple[SessionTaskOwner, ...]
assert issubclass(OwnerFollowingInputs, FollowingInputRetirement)
assert issubclass(PrivateSendClaim, FollowingPromptBoundary)
assert issubclass(TrackedTurnSession, FollowingTurnSource)
assert get_type_hints(TrackedTurnSession.__init__)["execution"] is NativeExecution
assert get_type_hints(TrackedTurnSession.execute)["prompt_send_boundary"] is FollowingPromptBoundary
assert get_type_hints(AgentCommsUpdate.acp_chunk)["return"]
assert get_type_hints(QueueScope.start_revision)["inputs"] is QueuePublication
assert get_type_hints(LiveQueueScope.start_revision)["inputs"] is QueuePublication
assert get_type_hints(PromptRequest.publish_acceptance)["inputs"] is QueuePublication
change_type = get_type_hints(Goals.goal_changed)["return"]
assert get_type_hints(change_type.publish)
execution_type = get_type_hints(GoalSnapshot)["execution"]
wake_execution_type = get_type_hints(WakeGoalContext)["execution"]
assert FieldCodec.value_schema(execution_type)
assert FieldCodec.value_schema(wake_execution_type)
assert set(execution_type.members_with(execution_type)) == set(
    wake_execution_type.members_with(wake_execution_type)
)
print(f"Goal publication contracts resolved after {sys.argv[1]}")
