"""Restored native sessions use per-input tools for goal and DM turns."""
import asyncio
import json

from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.native_tools import NativeToolDeclaration
from delivery_owner_fixture import reply_turn_finished


