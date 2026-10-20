"""A selected source fails before send once, through its existing execution store."""
import asyncio

import pytest

from agent_comms.activity import StoppedDrainDiagnostic
from agent_comms.coordination_cohort import pending_sealed_assignments
from agent_comms.coordinator import Coordination
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.selected_session import SelectedSession


