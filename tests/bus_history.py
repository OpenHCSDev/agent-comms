"""Tests read a bus's history through HistoryViews.history_page, the CLI's own reader."""

from agent_comms.channel_targets import CommandTarget
from agent_comms.channels import AnyChannel
from agent_comms.comms import Comms
from agent_comms.message_page import NEWEST_PAGE, PageBounds
from agent_comms.messages import Message


def history(comms: Comms, target: str = AnyChannel.declared_name, bounds: PageBounds = NEWEST_PAGE) -> list[Message]:
    """One page of target's history, every channel and direct message by default."""
    return list(comms.views.history_page(CommandTarget.named(target), bounds).messages)
