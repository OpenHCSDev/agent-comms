"""Goal claim_scope uses each follow-up's authority, including goal origin turns.

The backend event source is controlled here to exercise exact claim_scope races.
The callbacks, private goal ledger, ACP prompt queue, and transcript replay are real.
"""

import json
import logging
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from agent_comms.acp_failure import NativeTurnError
from acp.schema import TextContentBlock

import agent_comms.agent_events as ae
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    QueuePromptRequest,
    TranscriptSnapshotUpdate,
    decode_updates,
    encode_request,
)
from agent_comms.comms import wire
from agent_comms.goal_actions import ClearGoalAction, SetGoalAction
from agent_comms.goal_generation import ReadyGeneration, ReservedGeneration
from agent_comms.goals import NoGoal
from agent_comms.send_outcome import AuthorityChangedSend, GrantedSend
from agent_comms.owned_send import OwnedSend
from agent_comms.transcript_events import ContextTranscript
from delivery_owner_fixture import canonical_agent
from stored_inputs import stored_inputs


async def replay(agent):
    updates = []

    class Client:
        transcript_snapshots = True

        async def session_update(self, session_id, update):
            updates.append(update)

    await agent.sessions.transcript.replay("project", client=Client())
    page = next(
        update.page
        for update in decode_updates(updates[0].field_meta)
        if isinstance(update, TranscriptSnapshotUpdate)
    )
    return [item.text for item in page.events if not isinstance(item, ContextTranscript)]


