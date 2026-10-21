"""Install arguments retain the caller's paths across the target child's cwd."""
from pathlib import Path

from agent_comms.cli import build_parser
from agent_comms.cli_commands import InstallCliCommand
from agent_comms.commander.config import GivenRepos
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.launch import InstallRun


def test_install_paths_decode_before_the_target_child_changes_directory(
    private_directory, monkeypatch,
):
    home = private_directory / "home"
    home.mkdir()
    child = private_directory / "target"
    child.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(private_directory)
    arguments = build_parser().parse_args([
        "install", "--checkout", "checkout", "--project", "~/project",
        "--spill-dir", "backups", "--repos", "one, ~/two",
    ])
    command = InstallCliCommand.from_arguments(arguments)
    assert command.checkout == private_directory / "checkout"
    assert command.project == home / "project"
    assert command.spill_dir == private_directory / "backups"
    assert command.repos == GivenRepos((private_directory / "one", home / "two"))

    # This is the payload InstallRun.launch sends to the target's installer.
    install = InstallRun(command, private_directory / "bus", child, "a" * 40)
    payload = FieldCodec.encode_record(install)
    monkeypatch.chdir(child)
    assert FieldCodec.decode(InstallRun, payload) == install

    defaults = InstallCliCommand.from_arguments(build_parser().parse_args(["install"]))
    assert defaults.checkout == defaults.project == child
    assert defaults.spill_dir == Path.home() / ".cache/agent-rehearsal"
    assert defaults.spill_dir.is_absolute()
