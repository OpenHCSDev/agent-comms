"""The installed reader holds the bus lock across the new index publication."""
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.wire_log import WireLog
from public_message_index import CapturedMessages


def main():
    root, target_python, installer, root_id, directory = sys.argv[1:]
    root, directory = Path(root), Path(directory)
    directory.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    with WireLog(root / 'bus.jsonl').locked() as lock:
        source = lock.certified_read()
        if source.marker.root_id != root_id:
            raise ValueError('Public index installation selected another bus')
        CapturedMessages.capture(source, directory)
        subprocess.run(
            [target_python, installer, 'publish', str(root), str(directory), str(lock.descriptor)],
            env=environment, pass_fds=(lock.descriptor,), check=True,
        )


if __name__ == '__main__':
    main()
