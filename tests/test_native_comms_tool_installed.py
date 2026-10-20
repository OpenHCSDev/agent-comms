"""Real native coding and Comms execution share the existing checked call lifecycle."""

import asyncio
import json
import shlex
from pathlib import Path

from agent_comms.channel_coding_tools import CodingToolOwner
from agent_comms.wire_record import WireRecord
from delivery_owner_fixture import reply_turn_finished


