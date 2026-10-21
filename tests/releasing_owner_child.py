"""Real participant which releases on TERM but lets its caller control OS exit."""

import signal
import sys
import time
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.launch_root import LaunchRoot
from agent_comms.threads import current_thread


def main() -> None:
    (directory,) = sys.argv[1:]
    root = Path(directory)
    LaunchRoot.from_path(root).install()
    comms = Comms(root)

    def release(signum, frame):
        comms.owners.release('worker')
        (root / 'released').touch()
        while not (root / 'exit').exists():
            time.sleep(.01)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, release)
    comms.registry.declare(current_thread(comms.registry))
    (root / 'ready').touch()
    while True:
        signal.pause()


if __name__ == '__main__':
    main()
