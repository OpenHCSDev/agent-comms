"""Focused checks; actual commander injection still requires the installed hook."""

import io
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.send_guard import CommanderSession
from agent_comms.errors import ToolRefusedError
from agent_comms.todos import VerdictAudience
from agent_comms_hooks.watch import PeerSync


@pytest.fixture
def peer_config(commander_script, monkeypatch):
    order, _ = commander_script
    config = order.CONFIG.model_copy(update={"commander": "claude-code"})
    monkeypatch.setattr(order, "CONFIG", config)
    return order, config


def test_missing_empty_and_nonpeer_sends_are_overdue(peer_config):
    _, config = peer_config
    assert PeerSync.overdue(config)
    PeerSync.sent(config, "worker")
    assert not config.peer_sync_log.exists()
    config.peer_sync_log.parent.mkdir(parents=True, exist_ok=True)
    config.peer_sync_log.touch()
    assert PeerSync.overdue(config)


def test_successful_peer_send_is_fresh_but_same_minute_two_days_old_is_not(peer_config):
    _, config = peer_config
    PeerSync.sent(config, "desktop-commander")
    assert not PeerSync.overdue(config)
    assert config.peer_sync_log.read_text().split()[1:] == ["desktop-commander"]
    old = datetime.now().astimezone() - timedelta(days=2)
    os.utime(config.peer_sync_log, (old.timestamp(), old.timestamp()))
    assert PeerSync.overdue(config)


def test_wrong_target_does_not_make_the_log_fresh(peer_config):
    _, config = peer_config
    PeerSync.sent(config, "desktop-commander")
    config.peer_sync_log.write_text(f"{datetime.now():%H:%M} worker\n")
    assert PeerSync.overdue(config)


def test_shared_send_owner_records_once_and_never_on_failed_publication(peer_config, monkeypatch):
    order, config = peer_config
    monkeypatch.setattr(order, "call", lambda tool: SimpleNamespace(id="published"))
    assert order.post(VerdictAudience(("desktop-commander",)), "PRs needed: none") == "published"
    assert len(config.peer_sync_log.read_text().splitlines()) == 1

    def failed(tool):
        raise ToolRefusedError("publication refused")

    monkeypatch.setattr(order, "call", failed)
    with pytest.raises(ToolRefusedError):
        order.post(VerdictAudience(("desktop-commander",)), "PRs held: none")
    assert len(config.peer_sync_log.read_text().splitlines()) == 1


@pytest.mark.parametrize("event", PeerSync.events)
def test_overdue_hook_injects_or_blocks(peer_config, monkeypatch, capsys, event):
    _, config = peer_config
    monkeypatch.setattr(CommanderConfig, "installed", classmethod(lambda cls: config))
    monkeypatch.setattr(CommanderSession, "is_commander", lambda self: True)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"session_id": "test", "hook_event_name": event}))
    )
    PeerSync.main()
    output = json.loads(capsys.readouterr().out)
    if event == "Stop":
        assert output == {"decision": "block", "reason": PeerSync.warning}
    else:
        assert output["hookSpecificOutput"] == {
            "hookEventName": event,
            "additionalContext": PeerSync.warning,
        }


def test_noncommander_session_is_untouched(peer_config, monkeypatch, capsys):
    _, config = peer_config
    monkeypatch.setattr(CommanderConfig, "installed", classmethod(lambda cls: config))
    monkeypatch.setattr(CommanderSession, "is_commander", lambda self: False)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"session_id": "other", "hook_event_name": "Stop"}))
    )
    PeerSync.main()
    assert capsys.readouterr().out == ""
