"""Original decoder retains its opened certificate until target publication."""
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.wire_log import WireLog
from read_original_task_preimage import capture_original
from routing_recovery import write_original


def main():
    root, target_python, installer, root_id, receipt = sys.argv[1:]
    root, receipt = Path(root), Path(receipt)
    bus = WireLog(root / 'bus.jsonl')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    with bus.locked() as custody:
        source = custody.certified_read()
        source.require_marker(bus._private_marker_unlocked())
        if source.marker.root_id != root_id:
            raise ValueError('Original task carry root changed.')
        packet = capture_original(bus, source)
        encoded = json.dumps(packet, ensure_ascii=False)
        command = [target_python, installer, root, str(custody.descriptor), root_id, receipt]
        # Target validates every post-image under this SAME physical lock before
        # private preimages or any publication. A refusal leaves originals intact.
        subprocess.run([*map(str, command), '--validate'], input=encoded, text=True,
                       env=environment, pass_fds=(custody.descriptor,), check=True)
        source.require_current()
        originals = receipt.with_name(receipt.name + '.originals')
        originals.mkdir(mode=0o700)
        write_original(originals / 'bus.jsonl', bus.path.read_bytes())
        write_original(originals / 'bus_meta.json', bus.metadata_path.read_bytes())
        write_original(originals / 'private_bus_checkpoint.sqlite3',
                       (root / 'private_bus_checkpoint.sqlite3').read_bytes())
        write_original(originals / 'original-decoder.json', encoded.encode())
        for directory in (originals, originals.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        subprocess.run(list(map(str, command)), input=encoded, text=True,
                       env=environment, pass_fds=(custody.descriptor,), check=True)


if __name__ == '__main__':
    main()
