"""An install runs as its own transient systemd unit, so stopping its caller cannot kill it."""

import json
import os
import subprocess
import sys

import pytest

from agent_comms.cli_commands import InstallCliCommand
from agent_comms.commander import standing
from agent_comms.field_codec import FieldCodec, Id128
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.launch import (
    InstallLogs,
    InstallRun,
    InstallRunning,
    InstallUnit,
)
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName

FAKE = """#!{python}
import json, sys
with open({record!r}, "w") as record:
    json.dump(sys.argv[1:], record)
"""


@pytest.fixture
def systemd(tmp_path, monkeypatch):
    """A systemd-run on PATH that records its argv; ``show`` answers a collected unit."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "argv.json"
    fake = bin_dir / "systemd-run"
    fake.write_text(FAKE.format(python=sys.executable, record=str(record)))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(InstallUnit, "shown", staticmethod(lambda unit, name: "inactive"))
    return record


def test_spawn_starts_a_unit_whose_request_round_trips(tmp_path, systemd):
    root = tmp_path / "bus"
    root.mkdir()
    command = InstallCliCommand(checkout=tmp_path / "checkout", to="HEAD")
    run = InstallRun(command, root, tmp_path / "scratch", "a" * 40)

    run.spawn()

    argv = json.loads(systemd.read_text())
    assert f"--unit={run.unit.unit}" in argv
    assert run.unit.unit == f"agent-comms-install-{run.unit.identity}.service"
    # Values never reach a command line: systemd-run copies each declared name itself.
    settings = [word.removeprefix("--setenv=") for word in argv if word.startswith("--setenv=")]
    assert settings[:-1] == list(InstallUnit.inherited)
    assert settings[-1].startswith("PYTHONPATH=")
    assert f"--property=StandardInput=file:{run.unit.request}" in argv
    assert f"--property=StandardOutput=file:{run.unit.result}" in argv
    assert f"--property=StandardError=file:{run.unit.errors}" in argv
    assert "--property=KillMode=process" in argv
    assert argv[-3:] == [sys.executable, "-m", "agent_comms.installer"]
    assert FieldCodec.decode(InstallRun, FieldCodec.parse(run.unit.request.read_text())) == run


DECODED = """
import runpy
from agent_comms.installer.launch import InstallRun
from agent_comms.installer.refusals import UpgradeRefused

def run(self):
    raise UpgradeRefused(f"decoded {self.target}")

InstallRun.run = run
runpy.run_module("agent_comms.installer", run_name="__main__")
"""


def test_the_installed_launcher_hands_its_request_on_stdin(tmp_path):
    """Installs 5 and 6 died at IndexError: the live 878da2817 launcher pipes the request on
    stdin with no argument, and the tree it starts must read it there."""
    root = tmp_path / "bus"
    root.mkdir()
    run = InstallRun(InstallCliCommand(checkout=tmp_path / "checkout", to="HEAD"), root,
                     tmp_path / "scratch", "a" * 40)
    finished = subprocess.run(
        (sys.executable, "-c", DECODED), input=json.dumps(FieldCodec.encode(run)),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert finished.stderr.strip().splitlines()[-1].endswith(f"decoded {'a' * 40}")
    assert finished.returncode == 1


def test_a_launched_unit_gone_without_a_result_died(tmp_path, systemd):
    root = tmp_path / "bus"
    logs = InstallLogs(root)
    repo = RepositoryName.from_wire("OpenHCSDev/agent-comms")
    run = logs.minted()
    logs.record(run.logs, repo, "b" * 40)
    run.result.write_text("")
    run.errors.write_text("Traceback\nBrokenPipeError: [Errno 32]\n\n")

    assert logs.launched(repo, "b" * 40) == (run,)
    assert logs.member(repo, "b" * 40) is standing.DiedInstallDue
    assert run.last_error() == "BrokenPipeError: [Errno 32]"
    run.result.write_text("{}")
    assert logs.member(repo, "b" * 40) is standing.LaunchedInstallDue
    assert logs.member(repo, "c" * 40) is standing.PendingInstallDue


def test_a_missing_systemd_run_refuses_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="systemd-run"):
        InstallUnit(Id128.mint(), tmp_path).start(tmp_path, ("true",), "")


def test_a_second_launch_on_one_root_refuses_until_the_first_lets_go(tmp_path, systemd):
    """2026-10-24 18:54: three installs of one commit ran on one bus at once."""
    logs = InstallLogs(tmp_path / "bus")
    with (logs.launching(), pytest.raises(InstallRunning, match="another install holds"),
          logs.launching()):
        pass
    with logs.launching():
        pass


def running_only(monkeypatch, unit: InstallUnit) -> None:
    monkeypatch.setattr(InstallUnit, "shown", staticmethod(
        lambda name, field: "active" if name == unit.unit else "inactive"))


def test_every_merge_due_counts_the_running_install_of_its_target(tmp_path, systemd,
                                                                    monkeypatch):
    """The Core and Toad merges install one Core target, so the second merge's turn finds
    the first one's unit running and records itself on it instead of launching again."""
    root = tmp_path / "bus"
    root.mkdir()
    logs = InstallLogs(root)
    core = Repo(checkout=tmp_path / "checkout",
                github=RepositoryName.from_wire("OpenHCSDev/agent-comms"))
    toad = RepositoryName.from_wire("OpenHCSDev/toad")
    run = InstallRun(InstallCliCommand(checkout=core.checkout, to="HEAD"), root,
                     tmp_path / "scratch", "a" * 40)
    with logs.launching():
        run.spawn()
    logs.record(run.unit.logs, core.github, "a" * 40)
    running_only(monkeypatch, run.unit)

    standing.PendingInstallDue(toad, "c" * 40, "0" * 40, 1, 31, logs, core, "a" * 40).enforce(
        None, "commander")  # type: ignore[arg-type]
    standing.PendingInstallDue(toad, "d" * 40, "0" * 40, 1, 31, logs, core, "e" * 40).enforce(
        None, "commander")  # type: ignore[arg-type]

    assert logs.launched(toad, "c" * 40) == (run.unit,)
    assert logs.member(toad, "c" * 40) is standing.LaunchedInstallDue
    assert logs.launched(core.github, "a" * 40) == (run.unit,)
    assert logs.member(toad, "d" * 40) is standing.PendingInstallDue
    assert logs.units() == (run.unit,)


def test_retiring_old_logs_spares_a_running_units_directory(tmp_path, systemd, monkeypatch):
    """A fresh mint retired the running install's directory and killed it at 18:55."""
    logs = InstallLogs(tmp_path / "bus")
    live = logs.minted()
    running_only(monkeypatch, live)
    later = [logs.minted() for _ in range(StoreBackup.kept + 2)]

    assert live.logs.is_dir()
    assert sorted(unit.logs for unit in logs.units()) == sorted(
        [live.logs, *(unit.logs for unit in later[-StoreBackup.kept:])])
