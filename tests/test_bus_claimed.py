"""Claim attestation is one capability row in the message's SQLite transaction."""
from dataclasses import replace

import pytest

from agent_comms.bus import Bus, ClaimRecord, PublicRecord
from agent_comms.bus_tables import BusRow
from agent_comms.envelope_claim_transitions import ClaimRelease, ReleasedClaims
from agent_comms.errors import UnclaimedError
from agent_comms.messages import Claimed, InfoKind, Message


def claim_record(seq):
    message = Message("author", "worker", f"claim {seq}", InfoKind, seq=seq)
    transition = ReleasedClaims(
        owner=message.sender, incarnation="1", seq=seq, message_id=message.message_id,
        releases=(ClaimRelease("/owned/resource", "f" * 32),),
    )
    return ClaimRecord.of(message, transition)


def test_claimed_capability_lookup_and_rollback(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    history = PublicRecord.from_message(Message("author", "worker", "history", InfoKind))
    plain = bus.append(lambda seq, db: replace(history, seq=seq))
    with pytest.raises(UnclaimedError) as missing:
        bus.claim(plain.seq)
    assert missing.value.seq == plain.seq

    claimed = bus.append(lambda seq, db: claim_record(seq))
    assert bus.claim(claimed.seq) == claimed.claim_transition
    assert Bus(bus.path).claim(claimed.seq) == claimed.claim_transition
    with bus.observing() as db:
        (row,) = Claimed.table.select(db)
        assert row.seq == claimed.seq
        assert row.claim_transition == claimed.claim_transition

    aborted_seq = claimed.seq + 1
    with pytest.raises(RuntimeError):
        with bus.writing() as db:
            claim_record(aborted_seq).append_to(db, aborted_seq)
            raise RuntimeError("abort after both header and claim insertion")
    with bus.observing() as db:
        assert tuple(row.seq for row in BusRow.select(db, order_by=("seq",))) == (
            plain.seq, claimed.seq,
        )
        assert tuple(row.seq for row in Claimed.table.select(db)) == (claimed.seq,)
    with pytest.raises(UnclaimedError):
        bus.claim(aborted_seq)
