"""Run an install inside the runtime its bootstrap built (installer/bootstrap.py)."""

import json
import signal
import sys
from dataclasses import dataclass

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.installer.bootstrap import BuiltRuntime, stopped
from agent_comms.installer.launch import InstallRun
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.launch_root import LaunchRoot


@dataclass(frozen=True)
class PreparedInstall:
    """The launcher's request, still on stdin, and the runtime built for it."""

    request: InstallRun
    built: BuiltRuntime

    @classmethod
    def read(cls) -> PreparedInstall:
        return cls(FieldCodec.decode(InstallRun, FieldCodec.parse(sys.stdin.read())),
                   BuiltRuntime.argument())

    def run(self) -> None:
        self.request.phases.extend(self.built.phases)
        with LaunchRoot.from_path(self.request.root).selected():
            result = self.request.run(self.built.runtime)
            result.write_phases(sys.stderr)
            print(json.dumps(FieldCodec.encode(result), indent=2))


def main() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        PreparedInstall.read().run()
    except UpgradeRefused as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
