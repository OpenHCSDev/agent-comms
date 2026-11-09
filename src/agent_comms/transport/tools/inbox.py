"""The inbox read: one oldest-first page of untaken messages plus unresolved inputs.

`StandbyRequest`/`NoStandby` belong here because the standby review is an argument of this read
and has no other caller; the page it returns is `InboxPageRead`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from agent_comms.input_attempt import UnresolvedInputReference
from agent_comms.messaging.agent_inbox import InboxWait, NoWait, TakenInputPosition, TakenInputStart
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.channels import AnyChannel
from agent_comms.messaging.message_page import DisplayedMessagePage, InboxPage, PageBounds
from agent_comms.messaging.messages import Message
from agent_comms.nominal.field_codec import GoalId, NullMember, WireOmitDefault
from agent_comms.planning.goal_waits import NoStandbyReview, StandbyReview
from agent_comms.transport.request import tool_field
from agent_comms.transport.tool_caller import ToolCaller
from agent_comms.transport.tool_request import NativeToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class InboxPageRead(ToolResult):
    """One page of messages nothing took yet; reading it records nothing. A reader on the
    other side of the connector decodes it, so its messages are declared as messages, like a
    MessagePage's."""

    messages: tuple[Message, ...]
    remaining: int
    unresolved_inputs: tuple[UnresolvedInputReference, ...]
    standby_review: Annotated[StandbyReview | NoStandbyReview, WireOmitDefault()] = (
        NoStandbyReview()
    )


@dataclass(frozen=True)
class StandbyRequest:
    goal_id: Annotated[GoalId, *tool_field("Current goal for scoped standby review")]
    wait_for: Annotated[
        tuple[str, ...], *tool_field("Declared dependencies for standby review")
    ]

    def review(self, comms: Comms, thread: str) -> StandbyReview:
        return comms.goals.goal_input_review(thread, self.goal_id, self.wait_for)


@dataclass(frozen=True)
class NoStandby(NullMember):
    """The inbox read asked for no standby review."""

    def review(self, comms: Comms, thread: str) -> NoStandbyReview:
        return NoStandbyReview()


@dataclass(frozen=True, kw_only=True)
class CommsInboxTool(NativeToolRequest):
    label = "Comms Inbox"
    description = (
        "Fetch one oldest-first page of messages no harness attempted and no native input took, "
        "and the unresolved input attempts, for a thread. Optional target limits the conversation. "
        "Reading records nothing: a message leaves the page when its harness takes it, never "
        "because a page showed it, and human read markers are never touched. remaining counts "
        "the untaken messages after this page. One oversized message is returned alone for "
        "progress. Unresolved inputs remain UNKNOWN and are not paged; each is listed by its "
        "target, input ID and status. A bus input's ID names its bus sequence, where its body "
        "stays; a direct input has no bus row, and comms_input_delivery shows every "
        "unresolved input with its text. For standby, supply standby "
        "(goal_id and wait_for) to get standby_review: exact eligible keys and messages, "
        "already reviewed entries, and excluded owner/other-dependency inputs."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    target: Annotated[str, *tool_field("Channel or peer conversation")] = AnyChannel.address()
    limit: Annotated[int, *tool_field("Maximum messages in this page; must be positive")] = (
        100
    )
    standby: Annotated[
        StandbyRequest | NoStandby, *tool_field("Goal and dependencies to review")
    ] = NoStandby()
    after: Annotated[
        TakenInputPosition, *tool_field("Continue after this taken message; default the oldest")
    ] = TakenInputStart()
    wait: Annotated[
        InboxWait, *tool_field("Seconds to wait for a first message; default answer now")
    ] = NoWait()

    def apply(self, comms: Comms, caller: ToolCaller) -> InboxPageRead:
        thread = self.thread
        review = self.standby.review(comms, thread)
        page = self.page(comms)
        return InboxPageRead(
            page.page.messages, page.remaining, tuple(comms.goals.unresolved_inputs(thread)),
            standby_review=review,
        )

    def page(self, comms: Comms) -> InboxPage:
        return self.waited(comms, comms.bus.inbox_at)

    def messages(self, comms: Comms) -> tuple[Message, ...]:
        """The messages alone: a reader that shows no unresolved inputs and no remainder need
        not project them."""
        return self.waited(comms, comms.bus.untaken_at).page.messages

    def waited[Page: DisplayedMessagePage](self, comms: Comms, read: Callable[..., Page]) -> Page:
        if not (target := self.target.strip()):
            raise ToolRefusedError("An inbox target cannot be empty.")
        return self.wait.read(comms.bus.bus, lambda: read(
            self.thread, comms.registry.snapshot(), CommandTarget.named(target),
            after=self.after, bounds=PageBounds(limit=self.limit),
        ))
