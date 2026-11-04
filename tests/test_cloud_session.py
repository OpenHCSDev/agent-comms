"""Starting a cloud session asks `claude --cloud`, which refuses anything but a terminal."""
import json
import stat
import subprocess
import sys
from pathlib import Path


def started(tmp_path: Path, *arguments: str, exit_code: int = 0) -> subprocess.CompletedProcess[str]:
    """A stand-in claude records how it was run; the real one starts a paid session."""
    claude = tmp_path / "claude"
    claude.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"Path = {str(tmp_path / 'seen.json')!r}\n"
        "with open(Path, 'w') as seen:\n"
        "    json.dump(dict(argv=sys.argv[1:], terminal=os.isatty(1)), seen)\n"
        "print('View: https://claude.ai/code/session_x')\n"
        f"sys.exit({exit_code})\n")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    return subprocess.run(
        [sys.executable, "-c", "from agent_comms.cli_bridges.cloud_session import main; main()", *arguments],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "PYTHONPATH": ":".join(sys.path)},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False)


def test_a_session_starts_on_a_terminal_named_for_its_inbox(tmp_path: Path) -> None:
    result = started(tmp_path, "claude-remote-g")
    assert result.returncode == 0, result.stderr
    assert "https://claude.ai/code/session_x" in result.stdout
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen == {"argv": ["--cloud", "connect as claude-remote-g", "--name", "claude-remote-g"],
                    "terminal": True}


def test_a_task_follows_connecting(tmp_path: Path) -> None:
    started(tmp_path, "claude-remote-g", "fix the red tests")
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen["argv"][1] == "connect as claude-remote-g, then fix the red tests"


def test_a_failed_start_is_said_by_its_exit_code(tmp_path: Path) -> None:
    assert started(tmp_path, "claude-remote-g", exit_code=3).returncode == 3
