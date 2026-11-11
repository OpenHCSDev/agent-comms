from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import NameArgument, SessionAttachedResult
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.execution.child_process import NoProcess, ProcessIdentity, ProcessState
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True, kw_only=True)
class AttachSessionCliCommand(NameArgument, CliCommand, declared_name="attach-session"):
    help = "Attach a live Pi session to a registered thread"
    session_file: Annotated[str, Argument(flags=("--session-file",))]
    process: Annotated[
        ProcessState,
        Argument(
            flags=("--pid",),
            options={"type": ProcessIdentity.pid_argument},
        ),
    ] = dataclasses.field(default_factory=NoProcess)

    def apply(self, ctx: Comms) -> CliResult:
        current = ctx.registry.require(self.name)
        attached = ctx.threads.attach_session(
            current, self.session_file,
            process=self.process.applied(current.process_identity),
        )
        return SessionAttachedResult(attached.name, attached.session_file, attached.process_identity)
