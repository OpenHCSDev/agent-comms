"""Tests read a bus's history through HistoryViews.history_page, the CLI's own reader."""

from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.channels import AnyChannel
from agent_comms.comms import Comms
from agent_comms.messaging.message_page import (
    NEWEST_PAGE,
    PageBounds,
)
from agent_comms.messaging.messages import Message


def read_history(comms: Comms, target: str = AnyChannel.address(), bounds: PageBounds = NEWEST_PAGE) -> list[Message]:
    """One page of target's history, every channel and direct message by default."""
    return list(comms.views.history_page(CommandTarget.named(target), bounds).messages)
