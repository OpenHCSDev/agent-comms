"""Original info-kind claim envelopes remain readable committed history."""

import json
from pathlib import Path

import pytest

from agent_comms.field_codec import MalformedRecord
from agent_comms.messages import ClaimMessage, InfoKind, Message


def historical_row():
    return json.loads(
        Path(__file__).with_name("fixtures").joinpath("info-claim-1477.json").read_text()
    )


def test_original_info_claim_round_trips_without_rewriting():
    row = historical_row()
    message = Message.from_wire(row)
    assert isinstance(message, ClaimMessage)
    assert message.type is InfoKind
    assert message.require_claim_transition().seq == 1477
    assert message.to_wire() == row
    assert message.history_snapshot.to_wire().get("claim_transition") is None


def test_ordinary_info_still_round_trips():
    message = Message("author", "reader", "Ordinary info", InfoKind, seq=1)
    assert Message.from_wire(message.to_wire()) == message


def test_info_claim_decode_does_not_discard_unknown_fields():
    row = historical_row()
    row["claim_transition"]["unexpected"] = "not a declared claim field"
    with pytest.raises(MalformedRecord):
        Message.from_wire(row)
