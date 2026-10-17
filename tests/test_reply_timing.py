"""One configured timing value; ACP-local overrides never rewrite its source."""
from dataclasses import FrozenInstanceError

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.thread_configuration import ReplyTiming


def test_reply_timing_is_configured_once_and_overrides_are_session_local(tmp_path):
    service = Comms(tmp_path / 'wire', private_initial_writes=False, private_claim_writes=False)
    service.configuration.reply_timing = service.configuration.reply_timing.overridden(
        reply_window=17.25, no_reply_window=3.75, reply_quiet=0.125,
    )
    configured = service.configuration.reply_timing
    first = CommsAgent(service, reply_window=0.5, reply_quiet=0, auto_wake=False)
    second = CommsAgent(service, no_reply_window=0, auto_wake=False)
    assert service.configuration.reply_timing is configured
    assert configured == ReplyTiming(17.25, 3.75, 0.125)
    assert first.turns.reply_timing == ReplyTiming(0.5, 3.75, 0)
    assert second.turns.reply_timing == ReplyTiming(17.25, 0, 0.125)
    service.configuration.reply_timing = configured.overridden(reply_window=99)
    assert first.turns.reply_timing == ReplyTiming(0.5, 3.75, 0)
    assert second.turns.reply_timing == ReplyTiming(17.25, 0, 0.125)
    assert CommsAgent(service, auto_wake=False).turns.reply_timing == ReplyTiming(99, 3.75, 0.125)
    with pytest.raises(FrozenInstanceError):
        configured.reply_window = 99


def test_reply_timing_defaults_are_declared():
    assert ReplyTiming() == ReplyTiming(8.0, 2.5, 1.5)
