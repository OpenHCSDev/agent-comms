from __future__ import annotations

from abc import abstractmethod
import dataclasses
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.execution.launch_runtime import ThreadLaunch
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize


@dataclass(frozen=True)
class PollTarget(DeclaredFamily, affix="Target"):
    """Whose poll this is: the thread that calls, or one named on the command line."""

    @abstractmethod
    def target_name(self) -> str: ...


@dataclass(frozen=True)
class CallingThreadTarget(PollTarget):
    def target_name(self) -> str:
        return ThreadLaunch.current().name


@dataclass(frozen=True)
class NamedThreadTarget(PollTarget):
    name: str

    def target_name(self) -> str:
        return self.name

    @classmethod
    def argument(cls, value: str) -> dict[str, object]:
        return FieldCodec.encode_record(cls(value))


@dataclass(frozen=True, kw_only=True)
class PollCliCommand(CliCommand):
    help = "Status snapshot: thread, inbox, ledger, peers"
    # --thread names a thread; without it the poll is the calling thread's. Both parse to records.
    thread: Annotated[
        PollTarget,
        Normalize(dict),
        Argument(
            flags=("--thread",),
            options={"action": "store", "type": NamedThreadTarget.argument},
        ),
    ] = dataclasses.field(default_factory=CallingThreadTarget)

    def apply(self, ctx: CliBus) -> CliResult:
        return ctx.views.poll(self.thread)
