"""Run an install only after its bootstrap has built the matching dependencies."""

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Never
import signal
import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.launch import InstallRun, stopped
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.upgrade import Stack, TargetTree
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import ScratchCheckout
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.launch_root import LaunchRoot


@dataclass(frozen=True)
class PreparedInstall:
    """The bootstrap's exact built runtime, consumed only by the target interpreter."""

    request: InstallRun
    runtime: LaunchRuntime

    @classmethod
    def bootstrap(cls, request: InstallRun) -> Never:
        """Build before client carry discovery, then replace this process with that runtime.

        Stdin retains the install request and build timings across exec. The executable
        module comes from this declaration, never a separately maintained module spelling.
        """
        tree = TargetTree(request.command.checkout, request.scratch, request.target)
        try:
            with InstallCommands() as commands:
                StoreBackup.complete_interrupted(
                    request.root, request.command.spill_dir,
                    Stack(request.command.checkout, commands).restore_stores)
                staged = Stack.build(ScratchCheckout(tree.path), commands)
            request.phases.extend(timing.launched() for timing in commands.clock.phases)
            with tempfile.TemporaryFile(mode="w+") as incoming:
                incoming.write(FieldCodec.compact(FieldCodec.encode(cls(request, staged.runtime))))
                incoming.flush()
                incoming.seek(0)
                os.dup2(incoming.fileno(), sys.stdin.fileno())
            os.execve(staged.python, (str(staged.python), "-m", cls.__module__),
                      staged.environment(tree.steps_package))
        except BaseException:
            tree.__exit__(*sys.exc_info())
            raise


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
