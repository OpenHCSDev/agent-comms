"""The install unit: reads its ``InstallRun`` on standard input and prints the report."""
from __future__ import annotations

import signal
import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.launch import InstallRun, stopped
from agent_comms.launch_root import LaunchRoot


def main() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    request = FieldCodec.decode(InstallRun, FieldCodec.parse(sys.stdin.read()))
    try:
        with LaunchRoot.from_path(request.root).selected():
            request.bootstrap()
    except UpgradeRefused as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
