"""Real reserved inputs, source files and journals; no native/provider substitute."""
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import InputId
from agent_comms.notification_evidence import UnansweredResponse
from agent_comms.native_input_proof import NativeContextJournal, NativeContextProof
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.notification_evidence import AllOutcomes
from agent_comms.private_send_claim import PrivateSendClaim
from agent_comms.private_sidecar import native_request_digest
from agent_comms.selected_triage import AbsentTriageDecisionRecord, DecidedTriageRecord, IgnoreSelectedTriage
from native_proof_cases import append_input_entry, recorded_context, write_proof_rows
from test_coordinated_runtime import tmp_path  # noqa: F401
from test_native_failure_recovery import SentHistory, prepared_native_input, prepared_native_triage


async def recorded_input_context(claim: PrivateSendClaim) -> NativeContextProof:
    """Join the isolated raw writer before returning its retained session/journal proof."""
    def write() -> NativeContextProof:
        session, participant = claim.session, claim.selected
        SentHistory.write(claim, session, participant)
        identity = session.identity
        entry = 'input-before-answer'
        offset = append_input_entry(identity.path, {
            'type': 'message', 'id': entry,
            'timestamp': '2026-10-01T00:00:00.000Z',
            'message': {'role': 'user', 'inputId': claim.input_id,
                        'inputDigest': native_request_digest(claim.prompt.text),
                        'content': [{'type': 'text', 'text': claim.prompt.text}]},
        })
        record = {
            'schema': 2, 'type': 'context_committed',
            'inputId': claim.input_id, 'sessionId': identity.session_id,
            'sessionEntryId': entry, 'requestGeneration': 1,
            'llmContextDigest': 'f' * 64, 'committedAtMs': 1_791_964_800_001,
        }
        write_proof_rows(session.files(participant.comms.root), (record,))
        return recorded_context(identity.path, record, session_entry_start=offset)

    return await Coordination.run_worker(write)


async def acknowledge_context(claim: PrivateSendClaim, context: NativeContextProof) -> None:
    async with claim.selected.cursor_delivery.reading() as source_reads:
        await claim.context_committed(context, source_reads)


@pytest.mark.parametrize('source_count', (1, 3))
async def test_reserved_context_notifies_before_any_assistant_reply(tmp_path: Path, source_count: int):
    async with prepared_native_input(
        tmp_path, source_bodies=tuple(f'Grouped source {index}' for index in range(source_count - 1)),
    ) as claim:
        context = await recorded_input_context(claim)
        comms = claim.selected.comms
        originals = tuple(row.source for row in claim.stage.assignments)
        assert len(originals) == source_count
        before = comms.views.message_notifications_for_references(AllOutcomes(), originals)
        before_rows = tuple(row for rows in before.outcomes.values() for row in rows)
        assert before_rows and all(not row.state.consumed for row in before_rows)

        await acknowledge_context(claim, context)

        after = comms.views.message_notifications_for_references(AllOutcomes(), originals)
        after_rows = tuple(row for rows in after.outcomes.values() for row in rows)
        assert after_rows and all(row.state.consumed for row in after_rows)
        assert all(row.answer == UnansweredResponse() for row in after_rows)
        assert all(not row.displayed_to for row in after_rows)
        assert claim.stage.progress.current.starting
        with Coordination(claim.store_path) as store:
            original = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert original.require_context_proof() == context
            claim.commit(store, context)
            claim.commit(store, context)
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert retained == original


async def test_triage_consumption_precedes_and_survives_later_verdict(tmp_path: Path):
    async with prepared_native_triage(tmp_path) as claim:
        context = await recorded_input_context(claim)
        originals = tuple(row.source for row in claim.stage.assignments)
        before = claim.selected.comms.views.message_notifications_for_references(AllOutcomes(), originals)
        assert all(not row.state.consumed for rows in before.outcomes.values() for row in rows)
        await acknowledge_context(claim, context)
        active = claim.selected.comms.views.message_notifications_for_references(AllOutcomes(), originals)
        assert sum(row.state.consumed for rows in active.outcomes.values() for row in rows) == 1
        assert all(row.answer == UnansweredResponse() and not row.displayed_to
                   for rows in active.outcomes.values() for row in rows)
        with Coordination(claim.store_path) as store:
            original = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert original.verdict == AbsentTriageDecisionRecord()
            claim.commit(store, context)
            claim.stage.commit(store, claim.participant, claim.input_id, claim.token_digest,
                               context, IgnoreSelectedTriage())
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert retained.require_context_proof() == context
            assert retained.verdict == DecidedTriageRecord(IgnoreSelectedTriage)
            with store.session.writing() as db:
                with pytest.raises(IdentityConflict):
                    retained.commit_context(db, replace(context, llm_context_digest='e' * 64))
                with pytest.raises(sqlite3.IntegrityError):
                    NativeRuntimeInput.update(db, where='input_id=?', parameters=(claim.input_id,),
                                              verdict=AbsentTriageDecisionRecord())


async def test_triage_failure_keeps_previously_acknowledged_context_and_no_retry(tmp_path: Path):
    async with prepared_native_triage(tmp_path) as claim:
        context = await recorded_input_context(claim)
        await acknowledge_context(claim, context)
        with Coordination(claim.store_path) as store:
            claim.stage.fail_decision(store, claim.participant, claim.input_id, claim.token_digest, context)
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert retained.require_context_proof() == context
            assert retained.verdict == AbsentTriageDecisionRecord()
            with pytest.raises(IdentityConflict):
                claim.stage.reserve(store, claim.participant, claim.token_digest)
            assert len(NativeRuntimeInput.select(store.session._connection)) == 1


async def test_later_corroborated_request_preserves_first_context_receipt(tmp_path: Path):
    async with prepared_native_input(tmp_path) as claim:
        context = await recorded_input_context(claim)
        await acknowledge_context(claim, context)
        files = claim.session.files(claim.selected.comms.root)
        # Append through the declared journal owner, preserving its original file identity.
        with closing(sqlite3.connect(files.input_proof)) as db, db:
            first = NativeContextJournal.for_input(
                db, claim.input_id, range(context.request_generation, context.request_generation + 1))
            later = replace(first, request_generation=first.request_generation + 1,
                            llm_context_digest='d' * 64, committed_at_ms=first.committed_at_ms + 1)
            later.insert(db)
        later_context = later.at(context.session_file, session_entry_start=context.session_entry_start)
        await acknowledge_context(claim, later_context)
        with Coordination(claim.store_path) as store:
            claim.commit(store, later_context)
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert retained.require_context_proof() == context


async def test_same_request_contradiction_cannot_replace_context_receipt(tmp_path: Path):
    async with prepared_native_input(tmp_path) as claim:
        context = await recorded_input_context(claim)
        await acknowledge_context(claim, context)
        with Coordination(claim.store_path) as store:
            with pytest.raises(IdentityConflict):
                claim.commit(store, replace(context, llm_context_digest='e' * 64))
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert retained.require_context_proof() == context


@pytest.mark.parametrize('corrupt', (
    lambda context: replace(context, llm_context_digest='e' * 64),
    lambda context: replace(context, input_id=InputId('00000000000000000000000000000001')),
))
async def test_uncorroborated_live_context_never_grants_consumption(
    tmp_path: Path, corrupt: Callable[[NativeContextProof], NativeContextProof],
):
    async with prepared_native_input(tmp_path) as claim:
        context = await recorded_input_context(claim)
        with pytest.raises(IdentityConflict):
            await acknowledge_context(claim, corrupt(context))
        with Coordination(claim.store_path) as store:
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert not retained.reference.recorded
