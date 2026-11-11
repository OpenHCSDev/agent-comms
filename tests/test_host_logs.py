"""A cloud session reads the host's failures through one bounded read that never carries a secret."""
from pathlib import Path

import pytest

from agent_comms.transport.connector_caller import ThreadConnectorCaller
from agent_comms.transport.host_logs import (
    ConnectorHostLog,
    HostLog,
    InstallHostLog,
    ProcessesHostLog,
)
from agent_comms.transport.tools.served import CommsHostLogTool

GITHUB = "ghp_" + "a1b2c3d4" * 4
ANTHROPIC = "sk-ant-" + "a1b2c3d4" * 4


@pytest.mark.parametrize("line, shown", (
    ("opened wss://host.example.ts.net/watch/AbC-123_x now", "opened [inbox watch URL] now"),
    ("INFO root GET /watch/AbC-123_x 101 0.1s", "INFO root GET [inbox watch URL] 101 0.1s"),
    ("Authorization: Bearer abc.DEF-123", "Authorization: Bearer [hidden]"),
    ("POST /token?code=abcdef123456 200", "POST /token?code=[hidden] 200"),
    ('{"type":"probe","token":"9fb18ee9b7cb36bc"}', '{"type":"probe","token":"[hidden]"}'),
    ("token: short", "token: [hidden]"),
    ("exit code: 1", "exit code: 1"),
    ("HTTP status_code=500 session: 12", "HTTP status_code=500 session: 12"),
    ("tests passed: 12", "tests passed: 12"),
    ("plain line, nothing secret", "plain line, nothing secret"),
))
def test_a_secret_never_leaves_the_host(line, shown):
    assert HostLog.hidden(line) == shown


@pytest.mark.parametrize("line, secret", (
    (f"GITHUB_TOKEN={GITHUB}", GITHUB),
    (f"ANTHROPIC_API_KEY={ANTHROPIC}", ANTHROPIC),
    (f"cloning https://user:{GITHUB}@github.com/x.git", GITHUB),
    ("postgresql://admin:hunter2pass@db/x", "hunter2pass"),
    ("Authorization: Basic dXNlcjpodW50ZXIy", "dXNlcjpodW50ZXIy"),
    ("mysql -phunter2pass -h db", "hunter2pass"),
    (f"found {GITHUB} in the output", GITHUB),
    ("POST /token?access_token=abc%2Bdefghi%2Fjkl 200", "abc%2Bdefghi%2Fjkl"),
    ("--password=hunter2pass", "hunter2pass"),
    ("got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl back", "eyJhbGciOiJIUzI1NiJ9"),
    ("password: hunter2", "hunter2"),
    ("pass=hunter2", "hunter2"),
    ("set-cookie: session=abc123; Path=/", "abc123"),
    ("key AIza" + "SyA1b2c3d4" * 4, "AIzaSy"),
))
def test_the_secrets_a_host_really_holds_are_hidden(line, secret):
    assert secret not in HostLog.hidden(line)


def test_a_value_the_host_holds_is_hidden_wherever_it_appears():
    held = HostLog.held_values({"SERVICE_API_KEY": "plainwordsonly", "HOME": "/home/someone",
                                "SHORT_TOKEN": "abc"})
    assert held == ("plainwordsonly",)
    assert HostLog.hidden("then plainwordsonly came back", held) == "then [hidden] came back"


@pytest.mark.parametrize("name", ("MY_CREDENTIALS", "DB_PASS", "COOKIE_JAR"))
def test_a_secret_name_is_read_anywhere_in_the_variable(name):
    assert HostLog.held_values({name: "hunter2"}) == ("hunter2",)


def test_the_longer_held_value_is_hidden_whole():
    held = HostLog.held_values({"A_KEY": "abcdefgh", "B_KEY": "abcdefghijkl"})
    assert HostLog.hidden("abcdefghijkl", held) == "[hidden]"


def test_a_read_hides_every_line_a_member_returns(monkeypatch):
    monkeypatch.setattr(ConnectorHostLog, "lines", classmethod(lambda cls, root, count: (f"t={GITHUB}",)))
    assert GITHUB not in ConnectorHostLog.read(Path("."), 1)[0]


@pytest.mark.parametrize("lines", (-1, 0, 501))
def test_a_count_outside_the_bound_is_refused(lines):
    with pytest.raises(ValueError):
        CommsHostLogTool(source=ProcessesHostLog, lines=lines).apply(None, None)


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


def test_processes_are_named_by_program_and_never_by_their_arguments(tmp_path):
    header, *rows = ProcessesHostLog.lines(Path(tmp_path), 500)
    assert header.split()[-1] == "COMMAND"
    assert not any("--no-cov" in row or "faulthandler_timeout" in row for row in rows)


def test_a_session_on_a_grant_can_read_it():
    assert "comms_host_log" in ThreadConnectorCaller.exposed(object.__new__(ThreadConnectorCaller))
    assert CommsHostLogTool.read_only_hint
