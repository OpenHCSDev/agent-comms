"""The install unit: reads its ``InstallRun`` on standard input and prints the report."""
from __future__ import annotations

import json
import signal
import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.refusals import UpgradeRefused
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
            print(json.dumps(FieldCodec.encode(request.run()), indent=2))
    except UpgradeRefused as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
