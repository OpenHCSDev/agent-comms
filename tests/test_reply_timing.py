"""One configured timing value; ACP-local overrides never rewrite its source."""
from dataclasses import FrozenInstanceError

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.thread_configuration import ReplyTiming


def test_reply_timing_is_configured_once_and_overrides_are_session_local(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_COMMS_REPLY_WINDOW', '17.25')
    monkeypatch.setenv('AGENT_COMMS_NO_REPLY_WINDOW', '3.75')
    monkeypatch.setenv('AGENT_COMMS_REPLY_QUIET', '0.125')
    service = Comms(tmp_path / 'wire', private_initial_writes=False, private_claim_writes=False)
    configured = service.configuration.reply_timing
    first = CommsAgent(service, reply_window=0.5, reply_quiet=0, auto_wake=False)
    second = CommsAgent(service, no_reply_window=0, auto_wake=False)
    assert service.configuration.reply_timing is configured
    assert configured == ReplyTiming(17.25, 3.75, 0.125)
    assert first.turns.reply_timing == ReplyTiming(0.5, 3.75, 0)
    assert second.turns.reply_timing == ReplyTiming(17.25, 0, 0.125)
    monkeypatch.setenv('AGENT_COMMS_REPLY_WINDOW', 'invalid-after-construction')
    assert service.configuration.reply_timing is configured
    assert first.turns.reply_timing.reply_window == 0.5
    with pytest.raises(FrozenInstanceError):
        configured.reply_window = 99


def test_invalid_reply_timing_fails_at_configuration(monkeypatch):
    monkeypatch.setenv('AGENT_COMMS_REPLY_WINDOW', 'not-a-duration')
    with pytest.raises(ValueError):
        ReplyTiming()
