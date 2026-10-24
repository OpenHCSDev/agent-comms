"""An install runs as its own transient systemd unit, so stopping its caller cannot kill it."""

import json
import os
import sys

import pytest

from agent_comms.cli_commands import InstallCliCommand
from agent_comms.commander import standing
from agent_comms.field_codec import FieldCodec, Id128
from agent_comms.installer.launch import InstallLogs, InstallRun, InstallUnit
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
    assert f"--property=StandardOutput=file:{run.unit.result}" in argv
    assert f"--property=StandardError=file:{run.unit.errors}" in argv
    request = run.unit.logs / "request.json"
    assert argv[-4:] == [sys.executable, "-m", "agent_comms.installer", str(request)]
    assert FieldCodec.decode(InstallRun, FieldCodec.parse(request.read_text())) == run


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
