"""The real CLI dispatcher and its declaration-owned execution contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.cli_bridges.shared import Send
from agent_comms.cli_commands import CliCommand


@pytest.fixture
def isolated_cli(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # Set HOME before the child imports declarations with path defaults. Install's
    # default checkout is this non-Git directory, so it refuses before any install.
    env = {**os.environ, "HOME": str(home),
           "XDG_CONFIG_HOME": str(home / "config"),
           "XDG_STATE_HOME": str(home / "state"),
           "XDG_CACHE_HOME": str(home / "cache"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    def run(root, *arguments):
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.cli", "--root", str(root), *arguments],
            cwd=home, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode in (0, 1), result.stderr
        # Defaults can legitimately refuse a domain precondition (notably
        # install's non-Git checkout). Protocol TypeErrors must never be hidden.
        assert "TypeError:" not in result.stderr, (arguments, result.stderr)
        value = json.loads(result.stdout) if result.stdout.strip() else None
        assert value is None or isinstance(value, dict), (arguments, value)
        assert result.returncode != 0 or isinstance(value, dict), (arguments, result.stderr)
        return result.returncode, value

    return run


@pytest.mark.timeout(240)
def test_every_registered_command_runs_through_dispatcher_with_parser_defaults(tmp_path, isolated_cli):
    # Required arguments retain their genuine parser refusal. Optional defaults,
    # root selection, and execution are not replaced with a test adapter. Class
    # creation checks the resolved execute method even for required-argument cases.
    declarations = CliCommand.members_with(CliCommand)
    for declaration in declarations:
        isolated_cli(tmp_path / declaration.declared_name, declaration.declared_name)
    assert declarations


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
    for name in ("sender", "reader"):
        assert isolated_cli(root, "register", "--name", name,
                            "--worktree", str(tmp_path))[0] == 0
    body = "P363 real dispatcher publication"
    code, sent = isolated_cli(root, "send", "--from", "sender", "--to", "reader", "--body", body)
    assert code == 0
    code, history = isolated_cli(root, "history")
    assert code == 0
    entries = history["everything"]
    assert any(entry["id"] == sent["id"] and entry["text"] == body
               for entry in entries)
