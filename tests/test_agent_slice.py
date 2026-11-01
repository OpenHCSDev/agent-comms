"""The declared slice is installed once and shared by agent launch commands."""
import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.child_process import SystemdUserManager
from agent_comms.systemd_unit import AgentsSlice


@pytest.mark.parametrize(("cores", "quota"), [(12, 1000), (8, 600)])
def test_agent_slice_cpu_reservation(monkeypatch: pytest.MonkeyPatch, cores: int, quota: int) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    definition = AgentsSlice.unit_text()
    assert definition == f"[Slice]\nCPUQuota={quota}%\nCPUWeight=50\n"


def test_agent_slice_unknown_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    with pytest.raises(NotImplementedError, match="cannot determine number of cpus"):
        AgentsSlice.unit_text()


def test_agent_slice_launch_and_unchanged_install(tmp_path: Path, monkeypatch) -> None:
    reloads = []
    manager = SystemdUserManager(tmp_path / "runtime")

    def run(command, *, check, env):
        assert command == ("systemctl", "--user", "daemon-reload") and check
        assert env["XDG_RUNTIME_DIR"] == str(manager.runtime_directory)
        reloads.append(AgentsSlice.unit_file(tmp_path).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    definition = AgentsSlice.unit_text()
    assert "CPUQuota=" in definition
    command = manager.command(("/usr/bin/true",))
    assert f"--slice={AgentsSlice.unit}" in command
    assert command[:len(AgentsSlice.run_prefix())] == AgentsSlice.run_prefix()
    assert "--scope" in command
    assert command[-2:] == ("--", "/usr/bin/true")

    manager.prepare_units(home=tmp_path)
    installed = AgentsSlice.unit_file(tmp_path)
    assert installed.read_text(encoding="utf-8") == definition
    assert reloads == [definition]
    first_write = installed.stat().st_mtime_ns

    manager.prepare_units(home=tmp_path)
    assert installed.stat().st_mtime_ns == first_write
    assert reloads == [definition]

    installed.write_text("[Slice]\n", encoding="utf-8")
    manager.prepare_units(home=tmp_path)
    assert installed.read_text(encoding="utf-8") == definition
    assert reloads == [definition, definition]
