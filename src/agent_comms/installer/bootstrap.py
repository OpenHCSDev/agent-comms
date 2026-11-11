"""The install's first stage: build the target's locked runtime, then become it.

The launcher starts its own installer inside the target checkout, with the install
request still on stdin (InstallRun.spawn). Its current build policy chooses and checks
all target packages before old target code can refresh their versions. This stage imports
only installer-internal modules, then execs the checked runtime into its own target stage
(installer/prepared.py), which still finds the request on stdin. Carry remains owned by
the target runtime and the target checkout.
"""
from __future__ import annotations

import os
import signal
import sys
from dataclasses import dataclass
from typing import ClassVar, Never

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import LaunchedPhaseTiming
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.runtime_stack import RuntimeStack
from agent_comms.installer.target_tree import ScratchTree
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import ScratchCheckout


def stopped(signum: int, frame: object) -> None:
    """A stop runs the install's abort once; another signal cannot cut it short."""
    for each in (signal.SIGTERM, signal.SIGINT):
        signal.signal(each, signal.SIG_IGN)
    raise SystemExit(f"install: stopped by {signal.Signals(signum).name}")


@dataclass(frozen=True)
class BuiltRuntime:
    """The runtime the bootstrap built and its build timings, handed across exec as the
    target stage's one argument."""

    runtime: LaunchRuntime
    phases: list[LaunchedPhaseTiming]
    target_stage: ClassVar[str] = f"{__package__}.prepared"

    @classmethod
    def bootstrap(cls) -> Never:
        """Build before client carry discovery, then replace this process with that runtime."""
        tree = ScratchTree.entered()
        try:
            with InstallCommands() as commands:
                staged = RuntimeStack.build(ScratchCheckout(tree.path), commands)
            built = cls(staged.runtime, [timing.launched() for timing in commands.clock.phases])
            os.execve(staged.python,
                      (str(staged.python), "-m", cls.target_stage,
                       FieldCodec.compact(FieldCodec.encode(built))),
                      staged.environment(tree.steps_package))
        except BaseException:
            tree.__exit__(*sys.exc_info())
            raise

    @classmethod
    def argument(cls) -> BuiltRuntime:
        return FieldCodec.decode(cls, FieldCodec.parse(sys.argv[1]))


def main() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        BuiltRuntime.bootstrap()
    except UpgradeRefused as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
