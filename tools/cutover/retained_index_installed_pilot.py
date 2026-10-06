"""Joined authored old-source writer → target index; central batch is separate."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.store_files import _store_lock
from agent_comms.transcript_receipts import AssignedTranscriptSource
from agent_comms.wire_log import WireLog
from retained_index_cutover import RetainedIndexCutover


def main():
    stage = Path(os.environ['AC_INDEX_FIXTURE_STAGE'])
    stage.mkdir(parents=True, exist_ok=False)
    root = stage / 'wire'
    old_python = Path(os.environ['AC_INDEX_ORIGINAL_PYTHON'])
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    seeded = subprocess.run([
        str(old_python), str(Path(__file__).with_name('seed_retained_index_fixture.py')), str(root),
    ], env=environment, check=True, capture_output=True, text=True)
    seed = json.loads(seeded.stdout)
    log = WireLog(root / 'bus.jsonl')
    originals = {path: path.read_bytes() for path in
                 (root / 'registry.json', Path(seed['registry_guard']))}
    with log.path.open('rb') as stream:
        before = hashlib.file_digest(stream, 'sha256').hexdigest()
    try:
        with log.locked():
            pass
    except RelationViolationError as error:
        refusal = str(error)
    else:
        raise AssertionError('New writer unexpectedly admitted the original old schema')
    cutover = RetainedIndexCutover(old_python, seed['root_id'],
                                 Path(os.environ['AC_NATIVE_COPIED_PACKAGE']))
    # No fixture owner survives the old seed child. Existing real two-worker
    # gate owns busy/subset refusal, stop-all-before-operation and retained
    # distinct settings. This gate owns the incompatible installed writer seam.
    cutover.require_source()
    with _store_lock(root / 'wire'):
        cutover.quiet_install(root)
    assert all(path.read_bytes() == original for path, original in originals.items())
    with log.path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == before
    original_sender = FieldCodec.decode(ThreadIncarnation, seed['sender'])
    source = AssignedTranscriptSource(root, original_sender, log)
    rows = source.rows(limit=10)
    assert len(rows) == 1 and rows[0].message.to_wire() == seed['original']
    assert sorted(row.canonical_thread for row in rows[0].audience.recipients) == ['alpha', 'beta']
    assert [event.declared_name for event in source.events(rows[0])] == ['sent']
    receipt = {'old_python': str(old_python), 'new_python': sys.executable,
               'old_schema_refusal': refusal, 'original_bytes_unchanged': True,
               'original_frozen_sender_and_audience_unchanged': True,
               'original_registry_and_guard_unchanged': True,
               'central_batch_used': False, 'joined_authored_seed': True,
               'new_owner_launches': 0, 'provider_calls': 0,
               'original_input_replays': 0}
    (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
