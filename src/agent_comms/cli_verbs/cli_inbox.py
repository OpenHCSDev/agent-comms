from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_support.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import TargetEditResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import FieldCodec, RecordMember
from agent_comms.registry.threads import NativeOwnerCommand, OwnerLifecycleControl


@dataclass(frozen=True)
class ThreadExecutionResult(RecordMember, TargetEditResult):
    thread: str
    execution: str
    session_preserved: bool


@dataclass(frozen=True, kw_only=True)
class CliInboxCliCommand(
    NativeOwnerCommand,
    OwnerLifecycleControl,
    ThreadLifecycleCliCommand,
    CliCommand[ThreadExecutionResult],
    declared_name="cli-inbox",
):
    help = "Switch a native Pi thread onto a CLI inbox without changing its identity"

    def apply(self, ctx: Comms) -> ThreadExecutionResult:
        switched = ctx.threads.switch_to_cli_inbox(self.name)
        preserved = switched.session_file.location(switched.backend).is_resumable()
        return ThreadExecutionResult(
            thread=switched.name,
            execution=FieldCodec.encode(type(switched)),
            session_preserved=preserved,
        )
