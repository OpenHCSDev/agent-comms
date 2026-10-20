"""One completed native failure feeds notification and reported request failure.

The verified native CLI, HTTP provider, Core turn/application, input dispositions
and saved journal are real. Only HTTP response contents are controlled.
"""
import asyncio
import json
from pathlib import Path

import pytest

import agent_comms.native_pi as native_pi
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.acp_failure import ACPFailure, NativeMessageFailureSource, NativeTurnError
from compaction_loopback import LoopbackProvider
from delivery_owner_fixture import canonical_agent


