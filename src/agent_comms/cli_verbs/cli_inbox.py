from __future__ import annotations

from dataclasses import dataclass

from agent_comms.agent_backend import NewThreadBackend
from agent_comms.cli_support.named_thread import NamedThreadCliCommand
from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import TargetEditResult
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
    NamedThreadCliCommand,
    CliCommand[ThreadExecutionResult],
    declared_name="cli-inbox",
):
    help = "Switch a native Pi thread onto a CLI inbox without changing its identity"

    def apply(self, ctx: CliBus) -> ThreadExecutionResult:
        switched = ctx.threads.switch_to_cli_inbox(self.name)
        preserved = switched.session_file.location(NewThreadBackend.selected()).is_resumable()
        return ThreadExecutionResult(
            thread=switched.name,
            execution=FieldCodec.encode(type(switched)),
            session_preserved=preserved,
        )
