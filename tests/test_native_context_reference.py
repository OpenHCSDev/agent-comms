"""Original nullable SQL groups cannot mint a partial recorded native reference."""

from dataclasses import replace
import sqlite3

import pytest

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_input_record import NativeInputReference, UnrecordedNativeInputReference
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput


def cursor():
    return CurrentNativeCursor(
        wire_root_id="a" * 32, recipient_lookup="b" * 32, owner_thread="owner",
        owner_generation=1, owner_admission_generation=2, covered_seq=1, injected_seq=1,
        input_id="c" * 32, assignment_id="d" * 32, stage="full", session_id="original-session",
        request_generation=1,
    )


def reservation():
    return NativeRuntimeInput(
        input_id="c" * 32, assignment_id="d" * 32, stage="triage",
        owner_lookup="b" * 32, owner_thread="owner", owner_generation=1,
        owner_token_digest="e" * 64, execution_id=None, attempt_ordinal=None,
    )


def test_original_sql_null_check_cannot_mint_incomplete_reference():
    with sqlite3.connect(":memory:") as db:
        CurrentNativeCursor.create(db)
        original = cursor()
        original.insert(db)
        actual = CurrentNativeCursor.one(db, input_id=original.input_id)
        assert actual.reference == original.reference
        assert isinstance(actual.reference, NativeInputReference)
        # Reproduce the actual copied-native-fixture corruption: CHECK NULL
        # does not refuse this update. The original value owner must refuse it.
        db.execute("UPDATE current_native_cursor SET request_generation=NULL")
        malformed = CurrentNativeCursor.one(db, input_id=original.input_id)
        with pytest.raises(IdentityConflict, match="partial"):
            _ = malformed.reference


@pytest.mark.parametrize("changes", [
    {"input_id": None}, {"assignment_id": None}, {"stage": None},
    {"session_id": None}, {"request_generation": None},
    {"request_generation": 0}, {"request_generation": True},
])
def test_partial_original_cursor_never_becomes_unrecorded_or_complete(changes):
    with pytest.raises(IdentityConflict):
        _ = replace(cursor(), **changes).reference


def test_original_unrecorded_context_and_reserved_input_remain_distinct_from_corruption():
    empty = replace(cursor(), covered_seq=0, injected_seq=0, input_id=None,
                    assignment_id=None, stage=None, session_id=None, request_generation=None)
    assert empty.reference == UnrecordedNativeInputReference()
    assert reservation().reference == UnrecordedNativeInputReference()
    with pytest.raises(IdentityConflict, match="partial"):
        _ = replace(reservation(), request_generation=1).reference
    recorded = replace(reservation(), sent_owner_admission_generation=2,
                       session_id="original-session", session_file="/original/session.jsonl",
                       session_entry_id="original-entry", request_generation=1,
                       llm_context_digest="f" * 64)
    assert recorded.reference == NativeInputReference(
        recorded.input_id, recorded.assignment_id, recorded.stage, recorded.session_id, 1
    )
    for name in ("session_file", "session_entry_id", "llm_context_digest"):
        with pytest.raises(IdentityConflict, match="partial"):
            _ = replace(recorded, **{name: None}).reference
