"""Observe the accepted549 source with installed554; no input or replay."""
from dataclasses import replace
import hashlib
from importlib import metadata
import json
from pathlib import Path
import subprocess

import agent_comms
from agent_comms.coordination_errors import IdentityConflict, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.cursor_owner import CursorOwner
from agent_comms.field_codec import FieldCodec
from agent_comms.message_bus import MessageBus
from agent_comms.native_entries import NativeEvidenceScope
from agent_comms.native_runtime_input import CurrentNativeCursor
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.registration import Registration

here = Path(__file__).resolve().parent
checkout = here.parents[1]
root = Path('/home/ts/.cache/agent-scratch/m549/wire')
original = json.loads((checkout / 'evidence/certified-context-observations-20261003/installed-receipt.json').read_text())
protected = [Path(path) for path in original['protected_files_unchanged']]
protected += [Path('/home/ts/.cache/agent-scratch/m549/native-forks/2026-10-02T23-03-41-735Z_01a0fedb-efa7-71a3-8bb2-40907a55d91b.jsonl')]
protected += [Path(str(protected[-1]) + '.input-proof')]

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

before = {str(path): digest(path) for path in protected}
package = Path(agent_comms.__file__).parent
source = checkout / 'src/agent_comms'
assert not package.is_relative_to(source), 'installed reader must not import source checkout'
source_files = {str(path.relative_to(source)): digest(path) for path in source.rglob('*')
                if path.is_file() and '__pycache__' not in path.parts}
for name, expected in source_files.items():
    assert digest(package / name) == expected, name
(here / 'installed-source-proof.json').write_text(json.dumps(source_files, indent=2) + '\n')

registry = Registration(root / 'registry.json')
bus = MessageBus(root / 'bus.jsonl', registry, private_response_writes=True)
snapshot = registry.snapshot()
with bus.log.certified_read(blocking=False) as certified:
    root_id = certified.marker.root_id
with Coordination(root / 'coordination.sqlite3', lock_timeout=0) as store:
    with store.session.read():
        cursors = CurrentNativeCursor.select(store.session._connection)
    assert len(cursors) == 1
    cursor = cursors[0]
    owner = CursorOwner(
        thread=snapshot.require(cursor.owner_thread),
        admission_generation=cursor.owner_admission_generation,
        wire_root_id=root_id, generation=cursor.owner_generation,
    )
    reader = NativeSourceCursor(bus, store, wire_root_id=root_id)
    with NativeEvidenceScope() as evidence:
        coverage = reader._require_source(owner, cursor, reader._coverage(owner.lookup), evidence)
        assert coverage.covered_seq == cursor.covered_seq
        with store.session.read():
            db = store.session._connection
            # Same bounds perform no UPDATE and return the original SQL row.
            returned = reader._publish(db, owner, cursor, coverage,
                                       coverage.last_proof(cursor.injected_seq))
            assert returned is cursor
            try:
                owner.require_coverage(db, replace(cursor, covered_seq=coverage.covered_seq + 1), coverage)
            except IdentityConflict:
                excessive_prefix_refused = True
            else:
                raise AssertionError('unproved source prefix accepted')
            assert CurrentNativeCursor.select(db) == cursors
    # The original completed driver did not register this observing process.
    # Its proof is readable; its original admission is not transferable.
    try:
        reader.read(owner_name=cursor.owner_thread)
    except StaleFence as error:
        observing_process_refused = str(error)
    else:
        raise AssertionError('original owner admission transferred to observer')

after = {str(path): digest(path) for path in protected}
assert after == before
receipt = {
    'state': 'INSTALLED_ORIGINAL_CURSOR_PROOF_AND_CUSTODY_PASS',
    'source_head': subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip(),
    'core': str(package), 'sdk': metadata.version('agent-client-protocol'),
    'direct_url': json.loads(metadata.distribution('agent-comms').read_text('direct_url.json')),
    'source_files_verified': len(source_files), 'root': str(root),
    'original_cursor': FieldCodec.encode(cursor),
    'original_native_inputs_verified': len(coverage.inputs),
    'equal_bounds_exact_original_sql_row': True,
    'unproved_prefix_refused': excessive_prefix_refused,
    'original_admission_not_transferred': observing_process_refused,
    'protected_files_unchanged': after,
    'provider_calls': 0, 'native_inputs': 0, 'public_actions': 0,
    'scope': 'actual accepted549 native proof and changed cursor owner; no new ACP/native turn or public latency acceptance',
}
(here / 'installed-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({key: value for key, value in receipt.items()
                  if key not in ('protected_files_unchanged', 'direct_url')}, indent=2))
