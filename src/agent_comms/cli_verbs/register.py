from __future__ import annotations

from agent_comms.child_process import NoProcess
from agent_comms.child_process import ProcessIdentity
from agent_comms.child_process import ProcessState
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_commands import RegisteredResult
from agent_comms.cli_commands import _tags
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Editor
from agent_comms.field_codec import Normalize
from agent_comms.target_fields import ChoiceTargetField
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_execution import ThreadExecution
from agent_comms.thread_text import NoParent
from agent_comms.thread_text import NoThreadText
from agent_comms.thread_text import ThreadParent
from agent_comms.thread_text import ThreadText
from agent_comms.threads import Thread
from dataclasses import dataclass
from typing import Annotated
import dataclasses


@dataclass(frozen=True, kw_only=True)
class RegisterCliCommand(NameArgument, CliCommand):
    help = "Register a thread"
    worktree: Annotated[str, Argument(flags=("--worktree",))]
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
        type[ThreadExecution],
        Editor(ChoiceTargetField),
        Argument(
            flags=("--execution",),
        ),
    ] = ExternalThreadExecution

    def apply(self, ctx: Comms) -> CliResult:

        thread = Thread(
            name=self.name,
            tags=self.tags,
            worktree=self.worktree,
            parent=self.parent,
            task=self.task,
            process_identity=self.process,
            execution=self.execution,
        )
        ctx.registry.declare(thread)
        return RegisteredResult(self.name)
