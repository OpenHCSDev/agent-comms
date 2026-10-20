"""A real local RPC child pauses the owner before the fenced prompt write."""

from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.selected_session import SelectedSession
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_comms.coordinated_runtime as runtime
import agent_comms.native_pi as native_pi
from agent_comms.maintenance_barrier import DeadHolderReleasedPhase, MaintenanceHeldError
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_runtime_input import NativeRuntimeInput
from test_coordinated_runtime import _root


