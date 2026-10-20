"""Named private claim_scope refusals against production-created SQLite rows."""
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from binding_reads import binding_rows

import os
from dataclasses import replace

import pytest

from agent_comms.turn_lease import NoTurn

import agent_comms.private_send_rules as rules
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_input_record import TriageNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.child_process import ProcessIdentity
from agent_comms.turn_goal_permission import OwnerGoalPermission
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordinator import Coordination
from agent_comms.goals import Goal

from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_claim_epoch import RecordedNativeClaim
from agent_comms.private_sidecar import native_request_digest
from agent_comms.reservation_rules import ReservationRule, ReservationViolationError
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import UserThreadRole
from agent_comms.pi_vocabulary import ThinkingLevel
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


