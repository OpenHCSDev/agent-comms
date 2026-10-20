"""The install child: reads its ``InstallRun`` from stdin and prints the report."""
from __future__ import annotations

import json
import signal
import sys
from dataclasses import asdict

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.backup import UpgradeRefused
from agent_comms.installer.launch import InstallRun
from agent_comms.launch_root import LaunchRoot


def stopped(signum: int, frame: object) -> None:
    """A stop signal runs the same abort as a failure; a second one cannot cut that abort short."""
    for each in (signal.SIGTERM, signal.SIGINT):
        signal.signal(each, signal.SIG_IGN)
    raise SystemExit(f"install: stopped by {signal.Signals(signum).name}")


def main() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    request = FieldCodec.decode(InstallRun, FieldCodec.parse(sys.stdin.read()))
    try:
        with LaunchRoot.from_path(request.root).selected():
            print(json.dumps(request.run(), indent=2, default=asdict))
    except UpgradeRefused as error:
        raise SystemExit(error.output)


if __name__ == "__main__":
    main()
