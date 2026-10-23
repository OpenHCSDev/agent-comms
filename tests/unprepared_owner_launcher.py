"""Start one owner from a checkout copy that holds no prepared native package.

Run with that copy's ``src`` first on the import path: the launcher and the worker it
starts both derive their native resources from the copy, so the worker's launch fails
because the package is really missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.threads import Thread


def main(root: Path, name: str) -> None:
    comms = wire(root)
    comms.registry.declare(Thread(name, frozenset(), str(root)))
    print(comms.owners.start(name).pid)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
