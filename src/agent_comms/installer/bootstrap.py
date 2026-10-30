"""The install's first stage: build the target's locked runtime, then become it.

Any runtime's launcher, the outgoing one included, starts ``python -m agent_comms.installer``
inside the target tree with the tree's ``src`` on its path and the install request on stdin
(InstallRun.spawn). That interpreter predates the target's dependencies, so this stage
imports only installer-internal modules: it reads nothing of the request, builds the
runtime the tree's lock declares, and execs that runtime's interpreter into the target
stage (installer/prepared.py), which still finds the request on stdin.
"""
from __future__ import annotations

import os
import signal
import sys
from dataclasses import dataclass
from typing import ClassVar, Never

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import LaunchedPhaseTiming
from agent_comms.installer.runtime_stack import RuntimeStack
from agent_comms.installer.target_tree import ScratchTree
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import ScratchCheckout


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
