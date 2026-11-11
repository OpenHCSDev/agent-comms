"""The send declaration: how a caller addresses a thread or channel and what it publishes.

`SendTool` is the whole behaviour; `CommsSendTool` adds only the native call, so the pair is one
owner. Serving it for external callers stays in ``agent_comms.transport.tools.served``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Type

from agent_comms.coordination.tables.refusals import SendRefusal
from agent_comms.messaging.channel_targets import CommandTarget, NamedTarget
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import InfoKind, MessageKind, SendableMessageKind
from agent_comms.messaging.send_result import MessagePublished
from agent_comms.messaging.task_sources import NoTaskAttachment, TaskAttachment
from agent_comms.nominal.field_codec import WireChoices, WireName
from agent_comms.projects.change_report import NoChangeReport, ReportEvidence
from agent_comms.transport.native_tools import CommunicationCall
from agent_comms.transport.request import tool_field
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_request import PublishingTool, ThreadToolRequest
from agent_comms_hooks import ToolRefusedError

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True, kw_only=True)
class SendTool(PublishingTool[ThreadCaller, MessagePublished]):
    label = "Comms Send"
    description = (
        "Send a message to another thread or channel. Address specific threads with "
        "@thread-name in the body: the named threads are told they are the intended "
        "readers, and others dismiss quietly. Unknown names stay plain text. "
        "External sends may name received originals in answers; "
        "source-turn identity is not a model argument."
    )
    target: Annotated[str, *tool_field("Target thread name or channel", WireName("to"))]
    body: Annotated[str, *tool_field("Message text")]
    audience: Annotated[
        tuple[str, ...], *tool_field("Threads a channel message is for; no other member is woken")
    ] = ()
    type: Annotated[
        Type[MessageKind], *tool_field("Message kind", WireChoices(SendableMessageKind.names))
    ] = InfoKind
    report: Annotated[
        ReportEvidence,
        *tool_field("Change report fields; required for the change-report kind"),
    ] = NoChangeReport()

    answers: Annotated[
        tuple[MessageReference, ...],
        *tool_field("Original received messages explicitly answered; external sends without a named source turn must name these to link answers"),
    ] = ()

    def publish(self, comms: Comms, caller: ThreadCaller) -> MessagePublished:
        sender = caller.own_thread()
        target = CommandTarget.named(self.target)
        try:
            caller.require_directive(comms, self.body)
            body = target.addressed(self.audience, self.body)
            result = comms.messaging.send(
                sender, target.name, body, self.type, report=self.report,
                task=self.attachment(comms, target), origin=caller.origin(),
                answers=self.answers)
        except ToolRefusedError as error:
            # The caller hears the refusal now; the record waits for the wire lock on its own.
            threading.Thread(target=SendRefusal.record,
                             args=(comms.root, sender, self.target, error)).start()
            raise
        return MessagePublished.from_send(result)

    def attachment(self, comms: Comms, target: NamedTarget) -> TaskAttachment:
        """An ordinary send carries no authored task or selected request."""
        return NoTaskAttachment()


class CommsSendTool(SendTool, ThreadToolRequest):
    """The native send declaration; its fields and publication belong to SendTool."""

    native_call = CommunicationCall
