"""One fresh installed native journey covers delayed rounds and error settlement."""

from agent_comms.messages import NoticeRole
import asyncio
import json
from pathlib import Path

import pytest

from agent_comms.channel_coding_tools import CodingToolOwner, CodingToolSocket
from agent_comms.pi_events import MessageEnd, Response
from agent_comms.pi_payloads import ToolCallContent
from agent_comms.native_pi import NativePiTerminalFailure
from agent_comms.turn_failure import ModelRequestFailed
from agent_comms.tracked_turn import TrackedTurnSession
from delivery_owner_fixture import reply_turn_finished


