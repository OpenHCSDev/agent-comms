"""Canonical channel notifications reflect each recipient's recorded outcome.

The existing model-boundary fixture supplies responses. ACP routing, source
identity, SQLite, native journals and notification projection are not mocked.
"""

import asyncio
from dataclasses import replace

import pytest

import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.activity import NativeInputDrainDiagnostic, UnavailableDrainDiagnostic
from agent_comms.native_input_record import FullNativeExecution, TriageNativeExecution
from agent_comms.selected_triage import FullSelectedTriage, IgnoreSelectedTriage
from agent_comms.coordinator import Coordination
from agent_comms.coordination_errors import StaleFence
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.notification_progress import AnsweredProgress, IgnoredProgress, QueuedProgress, SentProgress
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.goal_actions import SetGoalAction
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import ReplayAssessments, ReplayFact
from delivery_owner_fixture import (
    canonical_delivery_owner,
    drain_diagnostic,
    native_model,
    reply_turn_finished,
)
from test_coordinated_runtime import tmp_path as private_root_fixture
from stored_inputs import stored_inputs

tmp_path = private_root_fixture


