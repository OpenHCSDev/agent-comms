"""Unstarted native input failure restores the exact original, without providers."""

from __future__ import annotations

import pytest

import agent_comms.agent_events as events
from agent_comms.coordinator import Coordination
from agent_comms.acp_extension import (
    InputFailedUpdate,
    decode_updates,
)
from stored_inputs import stored_inputs


