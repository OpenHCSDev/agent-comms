"""Read gate01 originals through their typed owners; never publish or replay.

This checks the cursor admission relation, not a current live-owner journey.
The original gate's owners are retired and its missing cursor remains missing.
Run from a qualified receiving installation for installed-path verification.
"""

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import agent_comms.cursor_owner as cursor_module
from agent_comms.acp_extension import EmptyCursorObservation
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.cursor_owner import CursorOwner
from agent_comms.native_entries import NativeEvidenceScope
from agent_comms.native_pi import NativeContextProof
from agent_comms.native_prompt_binding import PromptBinding, expected_prompt_matches_journal
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.registry_document import RegistryDocument


ROOT = Path('/home/ts/wt/b503c01/wire')
INPUT = 'd4eece9aee310bf4b9733ba3888e7008'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def readonly(path):
    db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def main():
    protected = [ROOT / name for name in (
        'coordination.sqlite3', 'native_prompt_bindings.sqlite3', 'registry.json', 'bus.jsonl'
    )]
    before = {str(path): digest(path) for path in protected}
    registry = RegistryDocument.from_wire(json.loads((ROOT / 'registry.json').read_text()))
    db = readonly(ROOT / 'coordination.sqlite3')
    binding_db = readonly(ROOT / 'native_prompt_bindings.sqlite3')
    try:
        original = NativeRuntimeInput.one(db, input_id=INPUT)
        binding = PromptBinding.one(binding_db, input_id=INPUT)
        assert binding.identity == original.identity
        context = NativeContextProof(
            original.input_id, original.session_id, original.session_entry_id,
            original.request_generation, original.llm_context_digest,
            Path(original.session_file),
        )
        journal_before = digest(context.session_file)
        assignments = original.execution.source_assignment_ids(db, INPUT)
        assert len(assignments) == 3
        assignment = WakeAssignment.one(db, assignment_id=assignments[-1])
        with NativeEvidenceScope() as reads:
            evidence = reads.for_source(context.session_file)
            assert NativeContextProof.read_evidence(
                context.session_file, INPUT, request_generation=context.request_generation,
                evidence=evidence,
            ) == context
            equality = expected_prompt_matches_journal(
                context.session_file, binding, evidence=evidence,
            )
            assert equality
        proof = original.execution.historical_proof(
            original, lifecycle=assignment.lifecycle,
            wire_root_id=binding.wire_root_id, source_seq=assignment.wire_seq,
            source_message_id=assignment.message_id, assignment_id=assignment.assignment_id,
            input_id=INPUT, owner_lookup=original.owner_lookup,
            owner_thread=original.owner_thread, owner_generation=original.owner_generation,
            context=context, native_reference=original.reference,
            expected_prompt_digest=binding.expected_prompt_digest,
            expected_prompt_equality_established=equality,
        )
        owner = CursorOwner(
            thread=registry.threads[original.owner_thread],
            admission_generation=original.sent_owner_admission_generation.value,
            generation=original.owner_generation, wire_root_id=binding.wire_root_id,
        )
        assert owner.admits(db, proof, proof.source_seq, None, None)
        assert owner.admits(db, proof, proof.source_seq, None, INPUT)
        assert not owner.admits(
            db, proof, proof.source_seq, None, '3c894db31874cf330849460b622f2d1d'
        )
        assert not replace(owner, admission_generation=owner.admission_generation + 1).admits(
            db, proof, proof.source_seq, None, None
        )
        assert EmptyCursorObservation().needs_refresh
        assert owner.cursor(db) is None
        assert digest(context.session_file) == journal_before
    finally:
        db.close()
        binding_db.close()
    assert before == {str(path): digest(path) for path in protected}
    print(json.dumps({
        'state': 'ORIGINAL_ADMISSION_RELATION_PASS',
        'module': cursor_module.__file__,
        'original_input': INPUT,
        'source_members': len(assignments),
        'controls': ['same_admission_without_transient_id', 'explicit_original_id',
                     'wrong_explicit_id_refused', 'new_admission_refused',
                     'empty_observation_requests_refresh'],
        'original_sql_bus_registry_journal_unchanged': True,
        'original_cursor_still_missing': True,
        'native_inputs': 0, 'provider_calls': 0, 'product_mutations': 0,
        'live_gate_qualification': False,
    }, indent=2))


if __name__ == '__main__':
    main()
