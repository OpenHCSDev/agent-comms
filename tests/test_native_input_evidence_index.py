"""A shared acquired journal can corroborate committed inputs in either order."""
import json

import pytest

from agent_comms.native_entries import NativeEvidenceScope
from agent_comms.native_pi import NativeContextProof
from agent_comms.native_session_reopen import NativeSessionIdentity
from native_proof_cases import write_proof_rows
from test_native_pi import INPUT_ID, _evidence


@pytest.mark.parametrize('newest_first', [True, False])
def test_shared_input_reader_indexes_the_whole_committed_cut(tmp_path, newest_first):
    session = _evidence(tmp_path)
    newer = 'e' * 32
    user = json.loads(session.read_text().splitlines()[1])
    user['id'] = 'newer'
    user['message']['inputId'] = newer
    offsets = {INPUT_ID: len(session.read_bytes().splitlines(keepends=True)[0]),
               newer: session.stat().st_size}
    with session.open('a') as stream:
        stream.write(json.dumps(user) + '\n')
    write_proof_rows(session, [dict(
        schema=2, type='context_committed', sessionId='sid', inputId=input_id,
        sessionEntryId=entry_id, requestGeneration=generation,
        llmContextDigest='c' * 64, committedAtMs=1_791_964_800_001 + generation,
    ) for generation, (input_id, entry_id) in enumerate(((INPUT_ID, 'entry'), (newer, 'newer')), 1)])
    before = session.read_bytes()
    with NativeEvidenceScope() as scope:
        evidence = scope.for_source(session)
        NativeSessionIdentity.from_evidence(evidence)
        order = (newer, INPUT_ID) if newest_first else (INPUT_ID, newer)
        proofs = [NativeContextProof.read_evidence(session, input_id, evidence=evidence,
                                                  session_entry_start=offsets[input_id])
                  for input_id in order]
        assert {proof.input_id for proof in proofs} == {INPUT_ID, newer}
        assert set(evidence.inputs) == {INPUT_ID, newer}
        assert evidence.tracked(INPUT_ID).id == 'entry'
        assert evidence.tracked(newer).id == 'newer'
    assert session.read_bytes() == before
