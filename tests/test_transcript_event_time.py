"""Missing or non-absolute saved record clocks fail at the native boundary."""

import pytest

from agent_comms.field_codec import MalformedRecord
from agent_comms.native_entries import NativeEntry


@pytest.mark.parametrize("stamp", [None, "not-a-time", "2026-09-28T23:12:33"])
def test_an_entry_without_an_absolute_recorded_clock_is_refused(stamp):
    with pytest.raises(MalformedRecord):
        NativeEntry.from_wire(
            {"type": "message", "timestamp": stamp, "message": {"role": "user", "content": "preserved"}}
        )
