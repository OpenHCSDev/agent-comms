"""One-use restart queue layout move; preserves every original record byte.

Run with the new Core decoder before publishing its queue writers. The old
watcher must have exited; an active watcher is not interrupted. No owners are
signalled, no request is attempted, and no state is reinterpreted or replayed.
"""

import argparse
import fcntl
import os
from pathlib import Path

from agent_comms.restart_queue import DIRECTORY, RestartQueue
from agent_comms.store_files import _store_lock


def move_restart_queue(root: Path) -> int:
    queue = RestartQueue(root / DIRECTORY)
    descriptor = os.open(queue.directory / 'watcher.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _store_lock(queue.lock_path):
            records = tuple((path, queue.read(path))
                            for path in sorted(queue.directory.glob('*.json')))
            destinations = set()
            for path, record in records:
                destination = queue.path_for(record)
                if (destination in destinations or destination.exists()
                        or destination.is_symlink()):
                    raise FileExistsError(f'Restart queue destination collision: {destination}')
                destinations.add(destination)
            for path, record in records:
                queue.move(path, record)
            return len(records)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    options = parser.parse_args()
    print(f'Moved {move_restart_queue(options.root)} restart records; none attempted')


if __name__ == '__main__':
    main()
