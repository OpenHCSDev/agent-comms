"""Original nullable SQL groups cannot mint a partial recorded native reference."""

from dataclasses import replace
import sqlite3

import pytest

from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.selected_triage import AbsentTriageDecisionRecord, DecidedTriageRecord, IgnoreSelectedTriage
from agent_comms.native_input_record import RecordedNativePosition
from agent_comms.native_input_record import NumberNativeColumn, TextNativeColumn, UnrecordedNativeColumn
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_input_record import NativeInputReference, UnrecordedNativeInputReference
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.native_claim_epoch import RecordedNativeClaim
from agent_comms.assignment_states import TriagePendingAssignment


def cursor():
    return CurrentNativeCursor(
        wire_root_id="a" * 32, recipient_lookup="b" * 32, owner_thread="owner",
        owner_generation=1, owner_claimed_generation=2, covered_seq=1, injected_seq=1,
        input_id=TextNativeColumn("c" * 32), stage=TextNativeColumn("full"),
        session_id=TextNativeColumn("original-session"), request_generation=NumberNativeColumn(1),
    )


def reservation():
    return NativeRuntimeInput(
        input_id="c" * 32, stage=TriageNativeExecution, execution_id=UnrecordedNativeColumn(), attempt_ordinal=UnrecordedNativeColumn(),
        owner_lookup="b" * 32, owner_thread="owner", owner_generation=1,
        owner_token_digest="e" * 64,
    )




@pytest.mark.parametrize("changes", [
    {"input_id": UnrecordedNativeColumn()}, {"stage": UnrecordedNativeColumn()},
    {"session_id": UnrecordedNativeColumn()}, {"request_generation": UnrecordedNativeColumn()},
    {"request_generation": NumberNativeColumn(0)}, {"request_generation": NumberNativeColumn(True)},
])
def test_partial_original_cursor_never_becomes_unrecorded_or_complete(changes):
    with pytest.raises(IdentityConflict):
        _ = replace(cursor(), **changes).reference


def test_original_unrecorded_context_and_reserved_input_remain_distinct_from_corruption():
    empty = replace(cursor(), covered_seq=0, injected_seq=0, input_id=UnrecordedNativeColumn(),
                    stage=UnrecordedNativeColumn(), session_id=UnrecordedNativeColumn(), request_generation=UnrecordedNativeColumn())
    assert empty.reference == UnrecordedNativeInputReference()
    assert reservation().reference == UnrecordedNativeInputReference()
    with pytest.raises(IdentityConflict):
        _ = replace(reservation(), request_generation=NumberNativeColumn(1)).reference
    recorded = replace(reservation(), sent_owner_claimed_generation=RecordedNativeClaim(2),
                       session_id="original-session", session_file="/original/session.jsonl",
                       session_entry_id=TextNativeColumn("original-entry"),
                       session_entry_start=RecordedNativePosition(1),
                       request_generation=NumberNativeColumn(1),
                       llm_context_digest=TextNativeColumn("f" * 64),
                       committed_at_ms=NumberNativeColumn(1_791_964_800_001))
    assert recorded.reference == NativeInputReference(
        recorded.input_id, type(recorded.execution), recorded.session_id, 1
    )
    with pytest.raises(IdentityConflict):
        _ = replace(recorded, session_file=None).reference
    for name in ("session_entry_id", "llm_context_digest", "committed_at_ms"):
        with pytest.raises(IdentityConflict):
            _ = replace(recorded, **{name: UnrecordedNativeColumn()}).reference


def test_original_native_sql_acquires_required_triage_or_full_recorded_proof():
    from dataclasses import fields
    from pathlib import Path
    from agent_comms.historical_native_inputs import (
        TriageHistoricalNativeInput, FullHistoricalNativeInput,
    )
    from agent_comms.native_pi import NativeContextProof
    from agent_comms.selected_triage import FullSelectedTriage

    recorded = replace(
        reservation(), sent_owner_claimed_generation=RecordedNativeClaim(2),
        session_id="original-session", session_file="/original/session.jsonl",
        session_entry_id=TextNativeColumn("original-entry"), request_generation=NumberNativeColumn(1),
        session_entry_start=RecordedNativePosition(1),
        llm_context_digest=TextNativeColumn("f" * 64), committed_at_ms=NumberNativeColumn(1_791_964_800_001),
    )
    source = dict(
        wire_root_id="a" * 32, source_seq=1, source_message_id="original-message",
        assignment_id="d" * 32, input_id=recorded.input_id,
        owner_lookup=recorded.owner_lookup, owner_thread=recorded.owner_thread,
        owner_generation=recorded.owner_generation,
        context=NativeContextProof(recorded.input_id, recorded.session_id,
            "original-entry", 1, "f" * 64, 1_791_964_800_001,
            Path(recorded.session_file), session_entry_start=1),
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
                               verdict=DecidedTriageRecord(decision))
            original.insert(db)
            assert db.execute("SELECT verdict FROM native_runtime_input WHERE input_id=?",
                              (original.input_id,)).fetchone() == (decision.declared_name.lower(),)
            actual = NativeRuntimeInput.one(db, input_id=original.input_id)
            proof_source = {**source, "input_id": original.input_id,
                            "context": replace(source["context"], input_id=original.input_id)}
            proof = actual.execution.historical_proof(actual, lifecycle=TriagePendingAssignment(), **proof_source)
            assert isinstance(proof, TriageHistoricalNativeInput)
            assert proof.assignment_id == source["assignment_id"]
            assert proof.decision is decision
            assert proof.proves_triage_source((proof,)) == (decision is IgnoreSelectedTriage)
            with pytest.raises(sqlite3.IntegrityError):
                NativeRuntimeInput.update(db, where="input_id=?", parameters=(original.input_id,),
                                          verdict=AbsentTriageDecisionRecord())
        full = replace(recorded, input_id="7" * 32,
                       stage=FullNativeExecution, **FullNativeExecution("5" * 32, 1).binding_fields(),
                       verdict=AbsentTriageDecisionRecord())
        full.insert(db)
        actual = NativeRuntimeInput.one(db, input_id=full.input_id)
        proof = actual.execution.historical_proof(actual, lifecycle=TriagePendingAssignment(),
                                                 **{**source, "input_id": full.input_id})
        assert isinstance(proof, FullHistoricalNativeInput)
        assert "decision" not in {item.name for item in fields(proof)}
        assert not proof.proves_triage_source((proof,))
        with pytest.raises((TypeError, ValueError)):
            replace(actual, verdict=DecidedTriageRecord(IgnoreSelectedTriage)).execution.historical_proof(
                replace(actual, verdict=DecidedTriageRecord(IgnoreSelectedTriage)), lifecycle=TriagePendingAssignment(), **source,
            )
        with pytest.raises(TypeError):
            TriageHistoricalNativeInput(execution=TriageNativeExecution(), **source)
