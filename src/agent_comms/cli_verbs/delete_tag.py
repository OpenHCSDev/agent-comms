from __future__ import annotations

import dataclasses
from dataclasses import dataclass, replace
from typing import Annotated, Self

from agent_comms.cli_support.exact_tag import ExactTagCliCommand
from agent_comms.cli_verbs.cli_command import MultipleTargets, TargetActionCommand
from agent_comms.cli_verbs.target_fields import ChoiceTargetField
from agent_comms.comms import Comms
from agent_comms.messaging.channel_management import (
    KeepThreadsTagDisposition,
    TagChangeResult,
    TagDisposition,
)
from agent_comms.nominal.field_codec import Argument, Editor, TargetBound


@dataclass(frozen=True, kw_only=True)
class DeleteTagCliCommand(MultipleTargets, ExactTagCliCommand, TargetActionCommand[TagChangeResult],
                          declared_name="delete-tag"):
    help = "Remove tag, archive tagged threads, or delete tagged threads"
    disposition: Annotated[
        TagDisposition,
        Editor(ChoiceTargetField),
        Argument(
            flags=("--disposition",), options={"help": "Choose what happens to tagged threads"}
        ),
    ] = dataclasses.field(default_factory=KeepThreadsTagDisposition)
    confirmed: Annotated[
        bool, TargetBound(), Argument(flags=("--confirmed",))
    ] = False

    def confirmation(self) -> str:
        return self.disposition.confirmation((self.name,))

    @classmethod
    def selection_confirmation[C: DeleteTagCliCommand](cls: type[C], commands: tuple[C, ...]) -> str:
        groups: dict[TagDisposition, list[str]] = {}
        for command in commands:
            groups.setdefault(command.disposition, []).append(command.name)
        return '\n'.join(disposition.confirmation(tuple(names))
                         for disposition, names in groups.items())

    def with_confirmation(self, confirmed: bool) -> Self:
        super().with_confirmation(confirmed)
        return replace(self, confirmed=confirmed)

    def apply(self, ctx: Comms) -> TagChangeResult:
        return ctx.channels.delete_tag(self.name, disposition=self.disposition, confirmed=self.confirmed)
