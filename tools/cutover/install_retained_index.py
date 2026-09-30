"""Current installer consumes an inherited original writer descriptor once."""
import fcntl
import json
import os
from pathlib import Path
import sys

from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.wire_log import WireLog


def require_retained_writer(root, descriptor, root_id):
    root = Path(root)
    custody = int(descriptor)
    opened = os.fstat(custody)
    named = (root / '.bus.jsonl.lock').stat()
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise ValueError('Inherited descriptor is not the original bus writer lock.')
    # Same opened description, nonblocking verification. A fresh lock acquire
    # would deadlock against the still-live old parent, and is forbidden here.
    fcntl.flock(custody, fcntl.LOCK_EX | fcntl.LOCK_NB)
    bus = WireLog(root / 'bus.jsonl')
    if bus.read_metadata_unlocked().root_id != root_id:
        raise ValueError('Original root identity changed before installation.')
    return bus


def main():
    root, descriptor, root_id = sys.argv[1:]
    bus = require_retained_writer(root, descriptor, root_id)
    witness = install_private_bus_checkpoint(bus, _bus_locked=True)
    print(json.dumps({'root_id': root_id, 'through_seq': witness.through_seq,
                      'retained_writer_descriptor_verified': True}), flush=True)


if __name__ == '__main__':
    main()
