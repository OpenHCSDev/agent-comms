"""The runtime being replaced starts the install, so the new installer reads what it sends."""

from agent_comms.installer.launch import InstallRun
from agent_comms.nominal.field_codec import FieldCodec


def test_the_new_installer_reads_the_request_the_installed_runtime_sent():
    """The exact keys core-6cbf66af3 wrote; before this, the first step of the install refused."""
    run = FieldCodec.decode(InstallRun, {
        "command": {
            "project": "/tmp/project", "repos": None, "commander": None, "kind": "install",
            "checkout": "/tmp/checkout", "to": "origin/main", "installer": None, "rehearse": False,
            "selected_inputs": [], "retire": False, "spill_dir": "/tmp/spill",
        },
        "phases": [], "root": "/tmp/root", "scratch": "/tmp/scratch", "target": "a" * 40,
    })
    assert run.command.to == "origin/main"
