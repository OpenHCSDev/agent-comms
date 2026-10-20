"""Native drafts borrow the turn's text and serialize their one worker connection."""
import asyncio
from functools import partial
import threading
from types import SimpleNamespace

from agent_comms.coordinator import Coordination
from agent_comms.external_transcript import DraftRow, ExternalTranscript, TurnDraftPublication
from agent_comms.native_transcript import NativeTranscript
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.turn_output import TurnOutput
from agent_comms.thread_identity import RecordedThreadIncarnation
import agent_comms.tracked_turn as tracked


