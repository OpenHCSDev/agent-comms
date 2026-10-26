"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_commands import TagRenamedResult
from agent_comms.cli_verbs.exact_tag import ExactTagCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from dataclasses import dataclass
from typing import Annotated
from typing import Self


@dataclass(frozen=True, kw_only=True)
class RenameTagCliCommand(ExactTagCliCommand, declared_name="rename-tag"):
    help = "Rename channel tag"
    new_name: Annotated[
        str, Argument(flags=("--to",), options={"help": "New tag name"})
    ]

    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name, new_name='')

    def apply(self, ctx: Comms) -> TagRenamedResult:
        ctx.channels.rename_tag(self.name, self.new_name)
        return TagRenamedResult(self.name, self.new_name)
