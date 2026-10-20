"""A real saved Pi child abandons its authenticated tool check on a scratch bus."""

import asyncio
import json
import signal

import pytest

from agent_comms.channel_coding_tools import CodingToolOwner
from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_tool_transport import ToolTransport
from agent_comms.pi_rpc import PiRpcChannel
from delivery_owner_fixture import reply_turn_finished


