"""The native observation family has metadata, never exempt retained content."""

import pytest

from agent_comms.compaction_states import (
    AbortedNoWriteNativeOutcome, CommittedNativeOutcome, NativeOutcome,
    UnknownNativeOutcome,
)
from agent_comms.retained_task_facts import RetainedTaskFacts
from agent_comms.private_path import FileIdentity, FileRevision


@pytest.mark.parametrize("outcome", [
    UnknownNativeOutcome("uncertain original"),
    CommittedNativeOutcome("entry", FileRevision(FileIdentity(1, 2), 3, 4, 5), "leaf", "0" * 64),
    AbortedNoWriteNativeOutcome(FileRevision(FileIdentity(1, 2), 3, 4, 5), "leaf"),
])
def test_native_outcome_metadata_uses_existing_frame_owner(outcome):
    assert NativeOutcome.read(outcome.journal_json()) == outcome


def test_unknown_reason_is_control_metadata_not_retained_content():
    outcome = UnknownNativeOutcome("x" * (RetainedTaskFacts.journal_control_bytes + 1))
    with pytest.raises(ValueError, match="control metadata"):
        outcome.journal_json()
