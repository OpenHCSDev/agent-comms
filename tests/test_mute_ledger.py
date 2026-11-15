"""The mute ledger: set, active-by-time, replace-not-duplicate, and clear."""

from pathlib import Path

from agent_comms.waking.mute_ledger import Mute, MuteLedger
from bus_record_fixture import RECIPIENT_LOOKUP, SENDER_LOOKUP


def test_mute_ledger_sets_expires_by_time_and_clears(tmp_path: Path) -> None:
    ledger = MuteLedger.at(tmp_path)
    assert ledger.muted(100.0) == frozenset()  # an absent file is no mutes.

    ledger.set(SENDER_LOOKUP, until=200.0)
    assert ledger.muted(100.0) == frozenset({SENDER_LOOKUP})
    assert ledger.muted(200.0) == frozenset()  # active is strict: until > now.
    assert ledger.muted(250.0) == frozenset()

    ledger.set(SENDER_LOOKUP, until=500.0)  # re-muting replaces, never duplicates.
    ledger.set(RECIPIENT_LOOKUP, until=500.0)
    assert ledger.muted(100.0) == frozenset({SENDER_LOOKUP, RECIPIENT_LOOKUP})
    assert len(ledger.read()) == 2

    ledger.clear(SENDER_LOOKUP)
    assert ledger.muted(100.0) == frozenset({RECIPIENT_LOOKUP})


def test_mute_active_is_strict_by_until() -> None:
    assert Mute(SENDER_LOOKUP, 10.0).active(9.0)
    assert not Mute(SENDER_LOOKUP, 10.0).active(10.0)
