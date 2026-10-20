"""Existing real native/ACP owner; inspect through its original Unix socket."""
from bus_history import read_history
from agent_comms.pi_vocabulary import OffThinkingLevel
from agent_comms.native_package import pinned_native_package

import asyncio
import os
import json
import sys
import time
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.cli_commands import ContextCliCommand
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.turn_context import (
    CompactionSummarySegment, CoordinationSegment, InjectionMessageSegment, SystemLayerSegment,
    ToolCatalogSegment, TranscriptSegment, UserFollowupSegment, UserInputSegment,
)
from agent_comms.threads import Thread
from agent_comms.goal_actions import GoalPrecondition, OwnerInvocable, RuntimeInvocable, SetGoalAction, StandbyGoalAction
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_entries import NativeEntry
from agent_comms.task_sources import CurrentTaskScopeSelection, OriginalTaskChange
from agent_comms.native_tools import NativeRequestTools
from delivery_owner_fixture import canonical_agent
from stored_inputs import stored_inputs



@pytest.mark.parametrize('recorded_readers', [True])
async def test_original_context_query_preserves_native_journal_and_dispatches_no_prompt(
    native_backend, recorded_readers
):
    from native_context_reader_journey import read_original_context

    await read_original_context(native_backend, recorded_readers=recorded_readers)

