"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms import field_codec
from agent_comms import messages
from agent_comms.change_report import NoChangeReport
from agent_comms.change_report import ReportEvidence
from agent_comms.declared_verb import DeclaredOptions
from agent_comms.field_codec import FieldCodec
from agent_comms.send_result import SendResult
from agent_comms.target_fields import ChoiceTargetField
from dataclasses import dataclass
from typing import Annotated
from typing import TYPE_CHECKING
import builtins

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True, kw_only=True)
class SendRequest(DeclaredOptions):
    help = "Send to a thread (DM), #channel, or #all"
    sender: Annotated[str, field_codec.Argument(flags=("--from",))] = ""
    target: Annotated[str, field_codec.Argument(flags=("--to",))]
    body: Annotated[str, field_codec.Argument(flags=("--body",))]
    type: Annotated[
        builtins.type[messages.MessageKind],
        field_codec.Editor(ChoiceTargetField),
        field_codec.Argument(
            flags=("--type",), options={"choices": messages.SendableMessageKind.names()}
        ),
    ] = messages.InfoKind
    role: Annotated[
        builtins.type[messages.SendableRole], field_codec.Argument(flags=("--role",))
    ] = messages.ConversationRole
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
