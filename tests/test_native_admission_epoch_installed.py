"""Real selected Pi send epoch, exact dead-owner recovery, and unrelated new input.

Only the localhost provider response and the owned native child exit are
controlled. Registry claim_scope, release acknowledgement, process identity, coordinator,
native input/proof journal and recovery monitor use their production owners.
"""

from bus_history import read_history
from agent_comms.pi_vocabulary import OffThinkingLevel
import asyncio
import hashlib
import json
import multiprocessing
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.assignment_states import CompletedAssignment
from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordinator import Coordination
from agent_comms.native_claim_epoch import RecordedNativeClaim
from agent_comms.native_pi import NativePiTerminalFailure, NativePiUnavailable
from compaction_loopback import LoopbackProvider
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from test_native_unknown_recovery import input_evidence
from test_selected_input_lifetime_native import (
    SelectedNativeJourney,
    configured_native_journey,
)


