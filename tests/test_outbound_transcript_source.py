"""Whole conversations derive their original frozen wire sources, in both directions."""

from agent_comms.presentation import AllOutcomes, OwnerOutcomes
from agent_comms.repo import Checkout
from agent_comms.channel_targets import CommandTarget

import sqlite3
import sys
from dataclasses import replace

import pytest

from agent_comms.channels import AnyChannel
from agent_comms.comms import wire
from agent_comms.coordination_errors import StaleRevision
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.notification_progress import (
    PendingProgress,
    SentProgress,
    UnrecordedProgress,
)
from agent_comms.pi_payloads import PublishedMessageToolDetails
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool
from agent_comms.transcripts import TranscriptRead
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from agent_comms.threads import Thread


def test_original_read_contention_is_unavailable_then_same_receipts_return(tmp_path):  # noqa: F811
    from agent_comms.coordination_errors import CoordinationReadUnavailable

    root, _root_id, comms, initial, _people = _root(tmp_path)
    database = root / "coordination.sqlite3"
    selection = AllOutcomes().selection((initial,))
    records = NotificationAssignment.resolve(NotificationAssignment.read(root, selection))
    assert records
    projected = tuple(NotificationAssignment.for_deliveries(selection, records))
    assert projected[0][0] is initial
    assert any(outcome is records[0] for outcome in projected[0][1])
    with pytest.raises(ValueError):
        tuple(NotificationAssignment.for_deliveries(selection, (*records, records[0])))
    # Duplicate evidences outside the acquired deliveries do not change their result.
    assert tuple(NotificationAssignment.for_deliveries(AllOutcomes().selection(()), (*records, records[0]))) == ()
