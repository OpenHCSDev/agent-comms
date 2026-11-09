"""The real CLI dispatcher and its declaration-owned execution contract."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from agent_comms.cli import CliInvocation
from agent_comms.cli_bridges.shared import Send
from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.messaging.bus import Bus


@pytest.fixture
def isolated_home(tmp_path):
    """HOME is set before the child imports declarations with path defaults."""
    home = tmp_path / "home"
    home.mkdir()
    return home, {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }


@pytest.fixture
def isolated_cli(isolated_home):
    home, env = isolated_home

    def run(root, *arguments):
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.cli", "--root", str(root), *arguments],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        # Every call here must succeed; a refusal names its own answer and stderr.
        assert result.returncode == 0, (arguments, result.stdout, result.stderr)
        # Protocol TypeErrors must never be hidden.
        assert "TypeError:" not in result.stderr, (arguments, result.stderr)
        assert result.stdout.strip(), (arguments, result.stderr)
        return json.loads(result.stdout)

    return run


@pytest.mark.timeout(240)
def test_every_registered_command_runs_through_dispatcher_with_parser_defaults(tmp_path, isolated_home):
    # Import once with HOME already isolated, then use the real dispatcher for
    # every declaration. Required options keep their genuine parser refusals;
    # install fails with Git's own error as one JSON result before any installation.
    home, env = isolated_home
    result = subprocess.run(
        [sys.executable, str(Path(__file__)), str(tmp_path)],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    # Selected imports may register a verb before full discovery. Every declaration
    # must still execute exactly once, regardless of either process's import order.
    assert sorted(json.loads(result.stdout)) == sorted(CliCommand.names())


def _exercise_registered_commands(directory: Path) -> None:
    visited = []
    for declaration in CliCommand.members_with(CliCommand):
        output = io.StringIO()
        root = directory / declaration.declared_name
        Bus.install(Bus.path_in(root))
        arguments = ['--root', str(root), declaration.declared_name]
        with redirect_stdout(output):
            code = CliInvocation().invoke(arguments, sys.stdout)
        assert code in (0, 1)
        json.loads(output.getvalue())
        visited.append(declaration.declared_name)
    print(json.dumps(visited))


def test_bridge_first_mro_is_refused_before_command_registration():
    with pytest.raises(TypeError, match="execute.*disagrees with CliExecution.execute"):

        class BridgeFirst(Send, CliCommand, declared_name=None):
            def apply(self, ctx):
                raise AssertionError("A mismatched command must never execute")


def test_async_override_of_sync_dispatch_is_refused_at_class_creation():
    with pytest.raises(TypeError, match="execute.*disagrees with CliExecution.execute"):

        class AsyncDispatch(CliCommand, declared_name=None):
            async def execute(self, route, output):
                return 0

            def apply(self, ctx):
                raise AssertionError("A mismatched command must never execute")


def test_real_send_publishes_and_history_retains_it(tmp_path, isolated_cli):
    root = tmp_path / "bus"
    Bus.install(Bus.path_in(root))
    for name in ("sender", "reader"):
        isolated_cli(root, "register", "--name", name, "--worktree", str(tmp_path))
    body = "P363 real dispatcher publication"
    sent = isolated_cli(root, "send", "--from", "sender", "--to", "reader", "--body", body)
    entries = isolated_cli(root, "history")["everything"]
    assert any(entry["id"] == sent["id"] and entry["text"] == body for entry in entries)


if __name__ == "__main__":
    _exercise_registered_commands(Path(sys.argv[1]))
