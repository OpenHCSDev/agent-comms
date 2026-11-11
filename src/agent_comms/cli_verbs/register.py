from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import NameArgument, _tags
from agent_comms.cli_verbs.cli_result import CliResult, RegisteredResult
from agent_comms.cli_verbs.target_fields import ChoiceTargetField
from agent_comms.comms import Comms
from agent_comms.execution.child_process import NoProcess, ProcessIdentity, ProcessState
from agent_comms.nominal.field_codec import Argument, Editor, Normalize
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_text import NoParent, NoThreadText, ThreadParent, ThreadText
from agent_comms.registry.threads import ExternalThread, Thread


@dataclass(frozen=True, kw_only=True)
class RegisterCliCommand(NameArgument, CliCommand):
    help = "Register a thread"
    worktree: Annotated[Checkout, Argument(flags=("--worktree",))]
    tags: Annotated[
        frozenset[str],
        Normalize(_tags),
        Argument(flags=("--tags",)),
    ] = dataclasses.field(default_factory=frozenset)
    parent: Annotated[ThreadParent, Argument(flags=("--parent",))] = NoParent()
    task: Annotated[ThreadText, Argument(flags=("--task",))] = NoThreadText()
    process: Annotated[
        ProcessState,
        Argument(
            flags=("--pid",),
            options={"type": ProcessIdentity.pid_argument},
        ),
    ] = dataclasses.field(default_factory=NoProcess)
    execution: Annotated[
        type[Thread],
        Editor(ChoiceTargetField),
        Argument(
            flags=("--execution",),
        ),
    ] = ExternalThread

    def apply(self, ctx: Comms) -> CliResult:

        thread = self.execution(
            name=self.name,
            tags=self.tags,
            worktree=self.worktree,
            parent=self.parent,
            task=self.task,
            process_identity=self.process,
        )
        ctx.registry.declare(thread)
        return RegisteredResult(self.name)
