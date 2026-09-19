import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.cli import main


@pytest.fixture
def run_cli(comms, monkeypatch):
    root = comms.root

    def run(*argv: str) -> tuple[int, dict]:
        code = main(["--root", str(root), *argv])
        captured = json.loads(Path("/dev/stdout").read_text()) if False else None
        return code, captured

    return run


@pytest.fixture
def cli(capsys):

    root = None

    def run(root_path, *argv: str) -> tuple[int, dict | None]:
        nonlocal root
        root = root_path
        code = main(["--root", str(root_path), *argv])
        captured = capsys.readouterr().out
        return code, (json.loads(captured) if captured.strip() else None)

    return run


class TestCliSuccess:
    def test_register_and_threads(self, cli, tmp_path):
        code, out = cli(tmp_path, "register", "--name", "a", "--worktree", "/wt", "--tags", "x,y")
        assert code == 0 and out == {"registered": "a"}
        code, out = cli(tmp_path, "threads")
        assert code == 0 and out["threads"][0]["name"] == "a"
        assert out["threads"][0]["tags"] == ["x", "y"]

    def test_send_and_inbox_and_ack(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        cli(tmp_path, "register", "--name", "b", "--worktree", "/wt")
        code, out = cli(
            tmp_path, "send", "--from", "a", "--to", "b", "--body", "hi", "--type", "alert"
        )
        assert code == 0 and "id" in out
        code, out = cli(tmp_path, "inbox", "--thread", "b")
        assert out["messages"][0]["text"] == "hi"
        assert out["messages"][0]["type"] == "alert"
        code, out = cli(tmp_path, "ack", "--thread", "b")
        assert out == {"acknowledged": 1}
        code, out = cli(tmp_path, "inbox", "--thread", "b")
        assert out == {"messages": []}

    def test_thread_detail_and_stop_and_heartbeat(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        code, out = cli(tmp_path, "thread", "--name", "a")
        assert out["name"] == "a" and out["status"] == "running"
        code, out = cli(tmp_path, "stop", "--name", "a")
        assert out == {"stopped": "a"}
        code, out = cli(tmp_path, "thread", "--name", "a")
        assert out["status"] == "stopped"
        code, out = cli(tmp_path, "heartbeat", "--name", "a")
        assert out == {"heartbeat": "a"}

    def test_ledger_read_and_merge(self, cli, tmp_path):
        merge_file = tmp_path / "merge.json"
        merge_file.write_text(json.dumps({"k": "v"}))
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        code, out = cli(tmp_path, "ledger", "--merge-from", str(merge_file), "--author", "a")
        assert out == {"merged": True}
        code, out = cli(tmp_path, "ledger")
        assert out["k"] == "v"

    def test_poll(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        cli(tmp_path, "register", "--name", "b", "--worktree", "/wt")
        cli(tmp_path, "send", "--from", "a", "--to", "b", "--body", "hi")
        code, out = cli(tmp_path, "poll", "--thread", "b")
        assert out["thread"]["name"] == "b" and len(out["inbox"]) == 1

    def test_fork_missing_session_reports_error(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        code, out = cli(tmp_path, "fork", "--name", "kid", "--parent", "a", "--task", "t")
        assert code == 1 and "session" in out["error"]


class TestCliFailClosed:
    def test_unknown_sender_is_error_exit_code(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "b", "--worktree", "/wt")
        code, out = cli(tmp_path, "send", "--from", "ghost", "--to", "b", "--body", "hi")
        assert code == 1 and "Sender" in out["error"]

    def test_unknown_thread_detail(self, cli, tmp_path):
        code, out = cli(tmp_path, "thread", "--name", "ghost")
        assert code == 1 and "not registered" in out["error"]

    def test_ledger_merge_requires_author(self, cli, tmp_path):
        merge_file = tmp_path / "merge.json"
        merge_file.write_text("{}")
        code, out = cli(tmp_path, "ledger", "--merge-from", str(merge_file))
        assert code == 1 and "author" in out["error"]

    def test_every_error_is_single_json_object(self, cli, tmp_path):
        code, out = cli(tmp_path, "thread", "--name", "ghost")
        assert isinstance(out, dict) and set(out) == {"error"}


class TestCliSubprocess:
    """The real adapter contract: exit code, stdout JSON, parseable by shim."""

    def test_console_script_roundtrip(self, tmp_path):
        env = dict(
            __import__("os").environ,
            PYTHONPATH=str(Path(__file__).parents[1] / "src"),
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_comms.cli",
                "--root",
                str(tmp_path),
                "register",
                "--name",
                "a",
                "--worktree",
                "/wt",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {"registered": "a"}
