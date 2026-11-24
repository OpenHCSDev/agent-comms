from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_support.named_thread import NamedThreadCliCommand
from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_inbox import ThreadExecutionResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.threads import ExternalOwnerCommand, OwnerLifecycleControl


@dataclass(frozen=True, kw_only=True)
class NativeThreadCliCommand(
    ExternalOwnerCommand,
    OwnerLifecycleControl,
    NamedThreadCliCommand,
    CliCommand[ThreadExecutionResult],
    declared_name="native-thread",
):
    help = "Switch an independent CLI-inbox thread onto native Pi without changing its identity"

    def apply(self, ctx: Comms) -> ThreadExecutionResult:
        switched = ctx.threads.switch_to_native(self.name)
        return ThreadExecutionResult(
            thread=switched.name,
            execution=FieldCodec.encode(type(switched)),
            session_preserved=switched.runtime.is_resumable(),
        )
