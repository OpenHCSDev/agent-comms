"""A cloud session reads the host's failures through one bounded read that never carries a secret."""
from pathlib import Path

import pytest

from agent_comms.connector_caller import ThreadConnectorCaller
from agent_comms.host_logs import HostLog, InstallHostLog, ProcessesHostLog
from agent_comms.tools import CommsHostLogTool


@pytest.mark.parametrize("line, shown", (
    ("opened wss://host.example.ts.net/watch/AbC-123_x now", "opened [inbox watch URL] now"),
    ("INFO root GET /watch/AbC-123_x 101 0.1s", "INFO root GET [inbox watch URL] 101 0.1s"),
    ("Authorization: Bearer abc.DEF-123", "Authorization: Bearer [hidden]"),
    ("POST /token?code=abcdef123456 200", "POST /token?code=[hidden] 200"),
    ('{"type":"probe","token":"9fb18ee9b7cb36bc"}', '{"type":"probe","token":"[hidden]"}'),
    ("token: short", "token: short"),
    ("plain line, nothing secret", "plain line, nothing secret"),
))
def test_a_secret_never_leaves_the_host(line, shown):
    assert HostLog.hidden(line) == shown


def test_the_newest_install_log_is_named_and_cut_to_its_tail(tmp_path):
    root = tmp_path / ".agent-comms"
    older, newer = (tmp_path / f"..agent-comms.installer-{name}" for name in ("old", "new"))
    for directory in (older, newer):
        directory.mkdir()
    (older / "stderr.log").write_text("old\n")
    (newer / "stderr.log").write_text("one\ntwo\nthree\n")
    assert InstallHostLog.lines(root, 2) == (str(newer / "stderr.log"), "two", "three")


def test_no_install_yet_is_an_empty_answer(tmp_path):
    assert InstallHostLog.lines(tmp_path / ".agent-comms", 5) == ()


def test_processes_answer_their_header_and_stop_at_the_count(tmp_path):
    lines = ProcessesHostLog.lines(Path(tmp_path), 3)
    assert len(lines) == 3 and lines[0].split()[0] == "PID"
    assert all(len(line) <= ProcessesHostLog.shown_columns for line in lines)


def test_a_session_on_a_grant_can_read_it():
    assert "comms_host_log" in ThreadConnectorCaller.exposed(object.__new__(ThreadConnectorCaller))
    assert CommsHostLogTool.read_only_hint
