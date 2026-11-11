from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from agent_comms.cli_verbs.declared_verb import DeclaredOptions
from agent_comms.cli_verbs.target_fields import ChoiceTargetField
from agent_comms.messaging.messages import (
    ConversationRole,
    InfoKind,
    MessageKind,
    SendableMessageKind,
    SendableRole,
)
from agent_comms.messaging.send_result import SendResult
from agent_comms.nominal import field_codec
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.change_report import NoChangeReport, ReportEvidence

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True, kw_only=True)
class SendRequest(DeclaredOptions):
    help = "Send to a thread (DM), #channel, or #all"
    sender: Annotated[str, field_codec.Argument(flags=("--from",))] = ""
    target: Annotated[str, field_codec.Argument(flags=("--to",))]
    body: Annotated[str, field_codec.Argument(flags=("--body",))]
    type: Annotated[
        builtins.type[MessageKind],
        field_codec.Editor(ChoiceTargetField),
        field_codec.Argument(
            flags=("--type",), options={"choices": SendableMessageKind.names()}
        ),
    ] = InfoKind
    role: Annotated[
        builtins.type[SendableRole], field_codec.Argument(flags=("--role",))
    ] = ConversationRole
    report: Annotated[
        ReportEvidence,
        field_codec.Argument(
            flags=("--report",),
            options={"help": "Change report fields as a JSON object", "type": FieldCodec.parse},
        ),
    ] = NoChangeReport()

    def sent(self, comms: Comms) -> SendResult:
        return comms.messaging.send(self.sender, self.target, self.body, self.type,
                                    role=self.role(), report=self.report)
