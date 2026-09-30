"""Actual old720 writer → installed new schema under the central quiet batch."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.transcript_receipts import AssignedTranscriptSource
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
    service = Comms(root)
    with service.bus.log.path.open('rb') as stream:
        before = hashlib.file_digest(stream, 'sha256').hexdigest()
    try:
        with service.bus.log.locked():
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
    assert service.owners.restart_owners(cutover=cutover) == ()
    with service.bus.log.path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == before
    original_sender = FieldCodec.decode(Thread, seed['sender'])
    rows = AssignedTranscriptSource.for_thread(root, original_sender, service.bus.log).rows(limit=10)
    assert len(rows) == 1 and rows[0].message.to_wire() == seed['original']
    assert sorted(row.canonical_thread for row in rows[0].audience.recipients) == ['alpha', 'beta']
    assert [event.declared_name for event in AssignedTranscriptSource.for_thread(
        root, original_sender, service.bus.log).events(rows[0])] == ['sent']
    receipt = {'old_python': str(old_python), 'new_python': sys.executable,
               'old_schema_refusal': refusal, 'original_bytes_unchanged': True,
               'original_frozen_sender_and_audience_unchanged': True,
               'central_batch_used': True, 'new_owner_launches': 0, 'provider_calls': 0,
               'original_input_replays': 0}
    (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
