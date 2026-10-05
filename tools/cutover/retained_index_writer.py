"""Executed by the original integer-OFD writer under stopped wire custody.

The installed source must supply that original lock contract. This is not a
current StoreLock adapter or a production decoder for old registry records.
"""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.wire_log import WireLog
from agent_comms.errors import RelationViolationError
from checkpoint_schema import declared_schema_digest


def bus_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    root, target_python, installer, root_id = sys.argv[1:]
    root = Path(root)
    log = WireLog(root / 'bus.jsonl')
    with log.locked() as custody:
        # The original writer's locked() validates its exact existing schema
        # and certificate before any mutation. No new reader interprets it.
        marker = log._private_marker_unlocked()
        if marker.root_id != root_id or marker.checkpoint_seal is None:
            raise RelationViolationError('Original private writer proof changed.')
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        target_schema = subprocess.run([
            target_python, str(Path(installer).with_name('checkpoint_schema.py')),
        ], env=environment, check=True, capture_output=True, text=True).stdout.strip()
        if target_schema == declared_schema_digest():
            raise RelationViolationError('Target schema no longer requires retained reset.')
        # Require the original integer-OFD contract before retiring its index.
        # A different source lock API must not fail only after that mutation.
        os.fstat(custody)
        before = bus_digest(log.path)
        retained = replace(marker, checkpoint_version=None, checkpoint_seal=None)
        log.write_metadata_unlocked(retained)
        (root / 'private_bus_checkpoint.sqlite3').unlink()
        subprocess.run([target_python, installer, root, str(custody), root_id],
                       env=environment, pass_fds=(custody,), check=True)
        after = log.read_metadata_unlocked()
        if replace(after, checkpoint_version=None, checkpoint_seal=None) != retained:
            raise RelationViolationError('Cutover changed original marker identity or admission.')
        if bus_digest(log.path) != before:
            raise RelationViolationError('Cutover changed original bus bytes.')


if __name__ == '__main__':
    main()
