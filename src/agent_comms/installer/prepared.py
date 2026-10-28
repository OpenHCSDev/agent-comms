"""Run an install only after its bootstrap has built the matching dependencies."""

import json
import signal
import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.launch import PreparedInstall, stopped
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.launch_root import LaunchRoot


def main() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    prepared = FieldCodec.decode(PreparedInstall, FieldCodec.parse(sys.stdin.read()))
    try:
        with LaunchRoot.from_path(prepared.request.root).selected():
            print(json.dumps(FieldCodec.encode(prepared.request.run(prepared.runtime)), indent=2))
    except UpgradeRefused as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
