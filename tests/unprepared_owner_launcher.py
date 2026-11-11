"""Start one owner from a checkout copy that holds no prepared native package.

Run with that copy's ``src`` first on the import path: the launcher and the worker it
starts both derive their native resources from the copy, so the worker's launch fails
because the package is really missing.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.owner_connection import OwnerReplyLostError


def main(root: Path, name: str) -> None:
    comms = wire(root)
    comms.registry.declare(NativeThread(name, frozenset(), Checkout(root)))
    # The worker dies on the invalid launch, taking the start's reply with it.
    with suppress(OwnerReplyLostError):
        comms.owners.start_many((name,))
    print(comms.registry.require(name).process_identity.pid)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
