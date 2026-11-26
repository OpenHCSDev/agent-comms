from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_comms.coordination.errors import CoordinationBeginUnavailable
from agent_comms.coordination.session import NativeCoordinationSession
from agent_comms.execution.native_runtime_input import SelectedToolSlot
from agent_comms.nominal.field_codec import SlotId, ToolCallId
from agent_comms.transport.tool_denial import SelectedToolDenied


def record_selected_terminal(
    coordination_path: Path, slot_id: SlotId, call_id: ToolCallId,
) -> None:
    """Only a committed terminal acknowledgement lets the tool return success."""
    try:
        with NativeCoordinationSession(str(coordination_path)) as session, session.writing() as db:
            SelectedToolSlot.require(db, slot_id=slot_id).acknowledge(db, call_id)
    except CoordinationBeginUnavailable:
        raise  # Nothing was written, so the acknowledgement is not UNKNOWN.
    except sqlite3.Error as error:
        raise SelectedToolDenied("Selected tool terminal acknowledgement UNKNOWN; no retry") from error
