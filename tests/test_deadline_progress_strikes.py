"""Deadline strikes consume real Git/PR progress through the retained order store."""
import time
from contextlib import nullcontext
from dataclasses import replace

from agent_comms.channel_targets import CommandTarget
from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.storage_rules import StorageRule
from agent_comms.todos import (
    MissedDeadlineStrike, NoCommitProgress, OpenPrProgress, OrderProgress,
    ProgressSource, PullRequests, PushedProgress, UnmergedPullRequest,
)
from test_order_progress import checkout, commit, git


def watch(comms):
    UnownedWheelSpin.watch_pass(lambda: nullcontext(comms))


def lines(comms):
    return [message.body for message in comms.views.history_page(
        CommandTarget.named('#comms'), viewer='lead').messages
        if message.body.startswith('Core strike')]


