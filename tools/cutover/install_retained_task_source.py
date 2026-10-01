"""Target declaration-owned attestation and full-stream installation, once."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.bus_activity_index import BusActivityIndex
from agent_comms.comms import Comms
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.store_files import _atomic_write_text
from agent_comms.wire_record import WireScan
from install_retained_index import require_retained_writer
from routing_recovery import write_original
from task_envelope_promotion import AuthoredTaskMemberPromotion


def prepare(bus, packet):
    marker = bus.read_metadata_unlocked()
    if packet['root_id'] != marker.root_id:
        raise ValueError('Original task preimage belongs to another root.')
    scan = WireScan(marker)
    changed, original_rows, target_rows = [], [], []
    offset = 0
    for entry in packet['rows']:
        raw = entry['raw'].encode('utf-8')
        if entry['offset'] != offset:
            raise ValueError('Original task preimage is not a complete contiguous stream.')
        offset += len(raw)
        public = entry['public']
        if public is not None and 'decision' in public:
            target = AuthoredTaskMemberPromotion.record(json.loads(raw), public, marker.root_id)
            serialized = json.dumps(target, ensure_ascii=False, allow_nan=False).encode() + b'\n'
            record = scan.read(serialized)
            changed.append(record.message.reference)
        else:
            serialized = raw
            scan.read(serialized)
        original_rows.append(raw)
        target_rows.append(serialized)
    original = b''.join(original_rows)
    if bus.path.read_bytes() != original:
        raise ValueError('Original opened task preimage changed before publication.')
    if scan.previous_sequence != packet['through_seq']:
        raise ValueError('Task carry changed the original admitted sequence frontier.')
    if not changed:
        raise ValueError('No retired task declaration requires this carry.')
    return marker, original, b''.join(target_rows), changed


def main():
    root, descriptor, root_id, receipt, *mode = sys.argv[1:]
    root, receipt = Path(root), Path(receipt)
    bus = require_retained_writer(root, descriptor, root_id)
    packet = json.load(sys.stdin)
    marker, original, target, changed = prepare(bus, packet)
    if mode == ['--validate']:
        print(json.dumps({'target_validated': True, 'changed_messages': len(changed)}), flush=True)
        return
    if mode:
        raise ValueError('Unknown task carry operation.')
    originals = receipt.with_name(receipt.name + '.originals')
    if (originals / 'bus.jsonl').read_bytes() != original:
        raise ValueError('Original bus preimage must be retained before publication.')
    if (originals / 'bus_meta.json').read_bytes() != bus.metadata_path.read_bytes():
        raise ValueError('Original admission marker preimage changed.')
    if receipt.exists():
        raise ValueError('Task carry receipt cannot be replaced.')
    # These resources contain positions/derivations of the old physical prefix,
    # not input outcomes. Candidate replay explicitly refuses an inode change
    # until the stopped operator retires its disposable database and WAL.
    candidate = WakeCandidateIndex(Comms(root).bus).path
    indices = [BusActivityIndex(bus.path).path, *root.glob('bus_display_*.json'),
               candidate, candidate.with_name(candidate.name + '-wal'),
               candidate.with_name(candidate.name + '-shm')]
    retired = []
    for path in indices:
        if path.is_symlink():
            raise ValueError('A derived index cannot be a link during task carry.')
        if path.exists():
            write_original(originals / path.name, path.read_bytes())
            retired.append(path)
    # Retire the OLD derived certificate before replacing bytes. Any uncertain
    # installation keeps owners stopped with original preimages; never auto-retry.
    retained = replace(marker, checkpoint_version=None, checkpoint_seal=None)
    bus.write_metadata_unlocked(retained)
    (root / 'private_bus_checkpoint.sqlite3').unlink()
    for path in retired:
        path.unlink()
    _atomic_write_text(bus.path, target.decode('utf-8'), fsync_parent=True)
    witness = install_private_bus_checkpoint(bus, _bus_locked=True)
    after = bus.read_metadata_unlocked()
    if replace(after, checkpoint_version=None, checkpoint_seal=None) != retained:
        raise ValueError('Task carry changed original marker identity or input admission.')
    if witness.through_seq != packet['through_seq']:
        raise ValueError('Installed task checkpoint changed the original sequence frontier.')
    result = {'state': 'RETAINED_TASK_SOURCE_CARRY_INSTALLED', 'root_id': root_id,
              'original_sha256': hashlib.sha256(original).hexdigest(),
              'target_sha256': hashlib.sha256(target).hexdigest(),
              'changed_references': [{'seq': r.seq, 'message_id': r.message_id} for r in changed],
              'through_seq': witness.through_seq, 'original_lock_retained': True,
              'retired_derived_indices': [path.name for path in retired],
              'native_writes': 0, 'input_replays': 0}
    write_original(receipt, (json.dumps(result, indent=2) + '\n').encode())
    descriptor = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
