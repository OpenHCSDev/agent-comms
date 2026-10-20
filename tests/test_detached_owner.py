"""Actual native owner survives client loss; a loopback provider gates completion."""
from agent_comms.native_package import pinned_native_package

import asyncio
import json
import os
from contextlib import suppress

import pytest
from acp.schema import SessionNotification

from agent_comms.acp import CommsClient
from agent_comms.acp_extension import CoordinationChangedUpdate, TurnChangedUpdate, decode_updates
from agent_comms.child_process import ParentedProcess
from agent_comms.comms import wire
from compaction_loopback import LoopbackProvider
from conftest import await_event_or_task


