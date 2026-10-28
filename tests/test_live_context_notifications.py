"""Real reserved inputs, source files and journals; no native/provider substitute."""
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import InputId
from agent_comms.native_pi import NativeContextProof
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.presentation import AllOutcomes
from agent_comms.private_send_claim import PrivateSendClaim
from agent_comms.text_digest import TextDigest
from native_proof_cases import append_input_entry, recorded_context, write_proof_rows
from test_coordinated_runtime import tmp_path  # noqa: F401
from test_native_failure_recovery import SentHistory, prepared_native_input


def recorded_input_context(claim: PrivateSendClaim) -> NativeContextProof:
    """Record the actual reserved prompt at its selected session's byte position."""
    session, participant = claim.session, claim.selected
    SentHistory.write(claim, session, participant)
    identity = session.identity
    entry = 'input-before-answer'
    offset = append_input_entry(identity.path, {
        'type': 'message', 'id': entry,
        'timestamp': '2026-10-01T00:00:00.000Z',
        'message': {'role': 'user', 'inputId': claim.input_id,
                    'inputDigest': TextDigest.of(claim.prompt.text).value,
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


@pytest.mark.parametrize('source_count', (1, 3))
async def test_reserved_context_notifies_before_any_assistant_reply(tmp_path: Path, source_count: int):
    async with prepared_native_input(
        tmp_path, source_bodies=tuple(f'Grouped source {index}' for index in range(source_count - 1)),
    ) as claim:
        context = recorded_input_context(claim)
        comms = claim.selected.comms
        originals = tuple(row.source for row in claim.stage.assignments)
        assert len(originals) == source_count
        before = comms.views.message_notifications_for_references(AllOutcomes(), originals)
        before_rows = tuple(row for rows in before.outcomes.values() for row in rows)
        assert before_rows and all(not row.state.consumed for row in before_rows)

        await claim.context_committed(context)

        after = comms.views.message_notifications_for_references(AllOutcomes(), originals)
        after_rows = tuple(row for rows in after.outcomes.values() for row in rows)
        assert after_rows and all(row.state.consumed for row in after_rows)
        assert all(row.answer is None for row in after_rows)
        assert all(not row.displayed_to for row in after_rows)
        assert claim.stage.progress.current.starting
        with Coordination(claim.store_path) as store:
            original = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert original.require_context_proof() == context
            claim.commit(store, context)
            claim.commit(store, context)
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert retained == original


async def test_same_request_contradiction_cannot_replace_context_receipt(tmp_path: Path):
    async with prepared_native_input(tmp_path) as claim:
        context = recorded_input_context(claim)
        await claim.context_committed(context)
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
        context = recorded_input_context(claim)
        with pytest.raises(IdentityConflict):
            await claim.context_committed(corrupt(context))
        with Coordination(claim.store_path) as store:
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert not retained.reference.recorded
