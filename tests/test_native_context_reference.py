"""Original nullable SQL groups cannot mint a partial recorded native reference."""

from dataclasses import replace
import sqlite3

import pytest

from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_input_record import NativeInputReference, UnrecordedNativeInputReference
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.native_admission_epoch import RecordedNativeAdmission
from agent_comms.assignment_states import TriagePendingAssignment


def cursor():
    return CurrentNativeCursor(
        wire_root_id="a" * 32, recipient_lookup="b" * 32, owner_thread="owner",
        owner_generation=1, owner_admission_generation=2, covered_seq=1, injected_seq=1,
        input_id="c" * 32, stage=FullNativeExecution, session_id="original-session",
        request_generation=1,
    )


def reservation():
    return NativeRuntimeInput(
        input_id="c" * 32, stage=TriageNativeExecution, execution_id=None, attempt_ordinal=None,
        owner_lookup="b" * 32, owner_thread="owner", owner_generation=1,
        owner_token_digest="e" * 64,
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
                    stage=None, session_id=None, request_generation=None)
    assert empty.reference == UnrecordedNativeInputReference()
    assert reservation().reference == UnrecordedNativeInputReference()
    with pytest.raises(IdentityConflict, match="partial"):
        _ = replace(reservation(), request_generation=1).reference
    recorded = replace(reservation(), sent_owner_admission_generation=RecordedNativeAdmission(2),
                       session_id="original-session", session_file="/original/session.jsonl",
                       session_entry_id="original-entry", request_generation=1,
                       llm_context_digest="f" * 64)
    assert recorded.reference == NativeInputReference(
        recorded.input_id, type(recorded.execution), recorded.session_id, 1
    )
    for name in ("session_file", "session_entry_id", "llm_context_digest"):
        with pytest.raises(IdentityConflict, match="partial"):
            _ = replace(recorded, **{name: None}).reference


def test_original_native_sql_acquires_required_triage_or_full_recorded_proof():
    from dataclasses import fields
    from pathlib import Path
    from agent_comms.historical_native_inputs import (
        TriageHistoricalNativeInput, FullHistoricalNativeInput,
    )
    from agent_comms.native_pi import NativeContextProof
    from agent_comms.selected_triage import FullSelectedTriage

    recorded = replace(
        reservation(), sent_owner_admission_generation=RecordedNativeAdmission(2),
        session_id="original-session", session_file="/original/session.jsonl",
        session_entry_id="original-entry", request_generation=1, llm_context_digest="f" * 64,
    )
    source = dict(
        wire_root_id="a" * 32, source_seq=1, source_message_id="original-message",
        assignment_id="d" * 32, input_id=recorded.input_id,
        owner_lookup=recorded.owner_lookup, owner_thread=recorded.owner_thread,
        owner_generation=recorded.owner_generation,
        context=NativeContextProof(recorded.input_id, recorded.session_id,
            recorded.session_entry_id, 1, "f" * 64, Path(recorded.session_file)),
        native_reference=recorded.reference,
        expected_prompt_equality_established=True,
    )
    with sqlite3.connect(":memory:") as db:
        NativeRuntimeInput.create(db)
        recorded.insert(db)
        # SQL NULL describes an original input without a recorded decision;
        # it cannot acquire a recorded triage proof.
        missing = NativeRuntimeInput.one(db, input_id=recorded.input_id)
        with pytest.raises((TypeError, ValueError)):
            missing.execution.historical_proof(missing, lifecycle=TriagePendingAssignment(), **source)
        for identity, decision in (("9", IgnoreSelectedTriage), ("8", FullSelectedTriage)):
            original = replace(recorded, input_id=identity * 32,
                               verdict=decision)
            original.insert(db)
            assert db.execute("SELECT verdict FROM native_runtime_input WHERE input_id=?",
                              (original.input_id,)).fetchone() == (decision.declared_name.lower(),)
            actual = NativeRuntimeInput.one(db, input_id=original.input_id)
            proof_source = {**source, "input_id": original.input_id,
                            "assignment_id": original.assignment_id,
                            "context": replace(source["context"], input_id=original.input_id)}
            proof = actual.execution.historical_proof(actual, lifecycle=TriagePendingAssignment(), **proof_source)
            assert isinstance(proof, TriageHistoricalNativeInput)
            assert proof.decision is decision
            assert proof.proves_triage_source((proof,)) == (decision is IgnoreSelectedTriage)
            with pytest.raises(sqlite3.IntegrityError, match="frozen"):
                NativeRuntimeInput.update(db, where="input_id=?", parameters=(original.input_id,),
                                          verdict=None)
        full = replace(recorded, input_id="7" * 32,
                       stage=FullNativeExecution, execution_id="5" * 32,
                       attempt_ordinal=1, verdict=None)
        full.insert(db)
        actual = NativeRuntimeInput.one(db, input_id=full.input_id)
        proof = actual.execution.historical_proof(actual, lifecycle=TriagePendingAssignment(),
                                                 **{**source, "input_id": full.input_id})
        assert isinstance(proof, FullHistoricalNativeInput)
        assert "decision" not in {item.name for item in fields(proof)}
        assert not proof.proves_triage_source((proof,))
        with pytest.raises((TypeError, ValueError)):
            replace(actual, verdict=IgnoreSelectedTriage).execution.historical_proof(
                replace(actual, verdict=IgnoreSelectedTriage), lifecycle=TriagePendingAssignment(), **source,
            )
        with pytest.raises(TypeError):
            TriageHistoricalNativeInput(execution=TriageNativeExecution(), **source)
