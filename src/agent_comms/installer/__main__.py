"""The install unit's bootstrap: builds the target's runtime and execs into it."""
from __future__ import annotations

import signal

from agent_comms.installer.bootstrap import BuiltRuntime, stopped
from agent_comms.installer.refusals import UpgradeRefused


def main() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        BuiltRuntime.bootstrap()
    except UpgradeRefused as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
